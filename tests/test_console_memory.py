import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from console_memory import diagnose, MIB, KV_STORAGE
spec = importlib.util.spec_from_file_location('memory_console', ROOT / 'scripts/local-console.py')
console = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console)


def evidence(reserve=8192, available=1800*MIB, parameters=None):
    p = parameters or dict(console.DEFAULTS, Budget=65536, Reserve=reserve, Prefill=min(reserve,8192),
                          KvType='int8', Concurrency=1, FastPrefill=True)
    e = {k:p[k] for k in ('Budget','Reserve','Prefill','SinkTokens','RecentTokens','Context')}
    r = min(e['Reserve'],4096); f = min(e['Prefill'],r)//128*128
    report = dict(version=1, required_bytes=3500*MIB, available_bytes=available,
        safety_bytes=128*MIB, planning_us=650,
        original={**{k:e[k] for k in ('Budget','Reserve','Prefill','Context')},
                  'Concurrency':p['Concurrency'],'kv_storage':KV_STORAGE[p['KvType']]}, options=[])
    for dtype,budget,cost in [('int8',20032,1400*MIB),('nvfp4',30016,1300*MIB),('rk8v4',25024,1350*MIB)]:
        fits = available >= cost+128*MIB
        report['options'].append(dict(id=dtype,available=fits,reason='' if fits else 'minimum_does_not_fit',
            budget=budget if fits else 0,reserve=r,prefill=f,
            fast_prefill=p.get('FastPrefill',False) if dtype!='nvfp4' else False,runtime_bytes=cost if fits else 0))
    log = f"FATAL server failed during startup | requested Engine runtime reservation requires {3500*MIB} bytes, but only {available} bytes are available for runtime capacity | KVMEM_MEMORY_PLAN "+json.dumps(report)
    return p, {'effective':e}, log


class Recommendations(unittest.TestCase):
    def test_native_candidates_and_reserve_policy(self):
        for reserve in (1024,4096,8192,32768):
            p,t,log=evidence(reserve)
            result=diagnose(log,p,t)
            self.assertEqual(result['basis'],'native_planner')
            self.assertEqual(result['planning_us'],650)
            self.assertEqual([o['changes']['Budget'] for o in result['options']],[20032,30016,25024])
            for o in result['options']:
                self.assertEqual(o['changes']['Reserve'],min(reserve,4096))
                self.assertEqual(o['safety_bytes'],128*MIB)

    def test_invalid_report_is_rejected_atomically(self):
        p,t,log=evidence(); prefix,raw=log.split(' | KVMEM_MEMORY_PLAN ')
        for mutation in (
            lambda d:d.update(version=2), lambda d:d.update(available_bytes=1),
            lambda d:d['original'].update(Budget=32768), lambda d:d['original'].update(kv_storage=4),
            lambda d:d['options'][1].update(budget=65537),lambda d:d['options'][1].update(budget=-64),
            lambda d:d['options'][1].update(reserve=512),lambda d:d['options'][1].update(prefill=8192),
            lambda d:d['options'][1].update(fast_prefill=True),
            lambda d:d['options'][1].update(runtime_bytes=1800*MIB)):
            d=json.loads(raw);mutation(d)
            result=diagnose(prefix+' | KVMEM_MEMORY_PLAN '+json.dumps(d),p,t)
            self.assertEqual(result['reason'],'invalid_native_plan')
            self.assertEqual(result['options'],[])

    def test_no_native_plan_never_guesses(self):
        p,t,log=evidence()
        for text in (log.split(' | KVMEM_MEMORY_PLAN ')[0],'CUDA error: out of memory','cudaErrorMemoryAllocation'):
            result=diagnose(text,p,t)
            self.assertEqual(result['reason'],'missing_native_plan')
            self.assertEqual(result['options'],[])

    def test_non_oom_and_recovered_probe_are_not_used(self):
        p,t,log=evidence()
        for text in ('invalid model','std::bad_alloc','CPU out of memory','CUDA invalid argument',
                     log+'\nFATAL server failed during startup | invalid model'):
            self.assertIsNone(diagnose(text,p,t))

    def test_no_legal_budget_is_retained_as_unavailable(self):
        p,t,log=evidence(available=600*MIB)
        result=diagnose(log,p,t)
        self.assertEqual(len(result['options']),3)
        self.assertTrue(all(not o['available'] and not o['changes'] for o in result['options']))

    def test_effective_values_used_for_unaligned_request(self):
        p,t,log=evidence();p.update(Budget=65599,Reserve=8200)
        self.assertEqual(diagnose(log,p,t)['original']['Reserve'],8192)

    def test_multi_gpu_does_not_receive_single_gpu_plan(self):
        p,t,log=evidence();p['Gpu']='GPU-a,GPU-b'
        self.assertEqual(diagnose(log,p,t)['reason'],'single_ninfer_only')

class ControllerRecovery(unittest.TestCase):
    def setUp(self):
        supported=patch.object(console,'AUTO_BUDGET_SUPPORTED',True)
        supported.start();self.addCleanup(supported.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = console.Controller(ROOT, self.tmp.name, 18199)
        p, t, log = evidence()
        self.c.active = console.normalize({'schema_version': 1, 'parameters': p}, self.c.backends)
        self.c.active['parameters']['ApiKey'] = 'keep-secret'
        self.c.active_plan = {'token_settings': t}
        self.c.exit_code = 1
        self.c.log_path.write_text(log)

    def request(self):
        return {'instance': self.c.instance, 'option': 'nvfp4',
                'configuration': console.without_secrets(self.c.active)}

    def test_stopped_clean_exit_and_post_ready_crash_have_no_suggestions(self):
        self.assertTrue(self.c.status()['memory_diagnosis']['options'])
        self.c.exit_code = 0
        self.assertNotIn('memory_diagnosis', self.c.status())
        self.c.exit_code = 1; self.c.launch_was_ready = True
        self.assertNotIn('memory_diagnosis', self.c.status())
        self.c.stop()
        self.assertNotIn('memory_diagnosis', self.c.status())

    def test_retry_preserves_secret_and_passes_through_normal_start(self):
        with patch.object(self.c, 'start') as start:
            self.c.retry_memory(self.request())
        updated = start.call_args.args[0]['parameters']
        self.assertEqual(updated['ApiKey'], 'keep-secret')
        self.assertEqual(updated['Reserve'], 4096)
        self.assertEqual(updated['KvType'], 'nvfp4')
        self.assertFalse(updated['FastPrefill'])
        self.assertEqual(updated['MaxTokens'], self.c.active['parameters']['MaxTokens'])
        self.assertNotIn('keep-secret', str(self.c.status()))

    def test_edited_or_stale_configuration_never_launches(self):
        for change in ('instance', 'config', 'option'):
            req = self.request()
            if change == 'instance': req['instance'] = 'stale'
            elif change == 'config': req['configuration']['parameters']['Model'] = 'other.ninfer'
            else: req['option'] = 'invented'
            with patch.object(self.c, 'start') as start, self.assertRaises(ValueError):
                self.c.retry_memory(req)
            start.assert_not_called()


if __name__ == '__main__': unittest.main()
