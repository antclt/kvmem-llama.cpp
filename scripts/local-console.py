"""Local, independently hosted configuration UI and owned inference-process controller.

Uses the selected checkout's canonical launcher in DryRun mode; never constructs
shell command strings. System telemetry uses psutil. Windows launch support.
"""
import argparse
import copy
import ctypes
import hashlib
from ctypes import wintypes
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_chat import ChatBridge, MAX_BODY
from console_hardware import SystemDiskSampler
from model_health import request_parameters, unchecked

ROOT = Path(__file__).resolve().parents[1]
SPECULATIVE_PROFILES = json.loads((ROOT / 'scripts/speculative-capabilities.json').read_text(encoding='utf-8-sig'))
DEFAULTS = dict(Backend='ninfer', Model='', Worker='', Gpu='', Context=262144,
                Budget=32768, Reserve=1024, HostMiB=16384, Prefill=128,
                MaxTokens=1024, Port=18200, ListenHost='127.0.0.1',
                SinkTokens=12288, RecentTokens=1024)
# Initial UI recipes; never merge engine-specific flags into saved configurations.
ENGINE_DEFAULTS = {'ninfer': {'Context': 262144,
            'Budget': 47104,
            'Reserve': 2048,
            'MaxTokens': 0,
            'ThinkingBudget': 16384,
            'ReasoningEffort': 'xhigh',
            'PreserveThinking': True,
            'HostMiB': 12288,
            'SinkTokens': 12288,
            'RecentTokens': 1024,
            'Concurrency': 1,
            'QueryScorer': 'page-softmax',
            'ScoreAggregation': 'mean',
            'GraphAlpha': 0.2,
            'Lrfu': False,
            'QueryMaxTokens': 512,
            'QueryReplayMaxTokens': 2048,
            'ThinkingMode': 'default',
            'KvType': 'int8',
            'Prefill': 256,
            'DraftTokens': 4,
            'DraftPolicy': 'adaptive',
            'NgramDrafts': 31,
            'NgramMinMatch': 12,
            'FastPrefill': True,
            'DeviceProfile': 'auto',
            'Vision': True,
            'VisionResidency': 'cpu',
            'VisionTokens': 1024,
            'Backend': 'ninfer',
            'SpeculativeMode': 'mtp',
            'RetainedSessions': 1},
 'llamacpp': {'Context': 262144,
              'Budget': 47104,
              'Reserve': 2048,
              'MaxTokens': 0,
              'ThinkingBudget': 16384,
              'ReasoningEffort': 'xhigh',
              'PreserveThinking': True,
              'HostMiB': 12288,
              'SinkTokens': 12288,
              'RecentTokens': 1024,
              'Concurrency': 1,
              'QueryScorer': 'page-softmax',
              'ScoreAggregation': 'mean',
              'GraphAlpha': 0.2,
              'Lrfu': False,
              'QueryMaxTokens': 512,
              'QueryReplayMaxTokens': 2048,
              'ThinkingMode': 'on',
              'KvTypeK': 'q8_0',
              'KvTypeV': 'q8_0',
              'Prefill': 256,
              'DraftTokens': 3,
              'DraftPolicy': 'fixed',
              'Vision': True,
              'VisionResidency': 'cpu',
              'VisionTokens': 1024,
              'Backend': 'llamacpp',
              'SpeculativeMode': 'mtp',
              'RetainedSessions': 1},
 'polystrata': {'Context': 262144,
                'Budget': 47104,
                'Reserve': 2048,
                'MaxTokens': 0,
                'ThinkingBudget': 16384,
                'ReasoningEffort': 'xhigh',
                'PreserveThinking': True,
                'HostMiB': 12288,
                'SinkTokens': 12288,
                'RecentTokens': 1024,
                'Concurrency': 1,
                'QueryScorer': 'page-softmax',
                'ScoreAggregation': 'mean',
                'GraphAlpha': 0.2,
                'Lrfu': False,
                'QueryMaxTokens': 512,
                'QueryReplayMaxTokens': 2048,
                'ThinkingMode': 'on',
                'KvType': 'q8_0',
                'Prefill': 512,
                'DraftTokens': 2,
                'DraftPolicy': 'adaptive',
                'Vision': False,
                'VramReserveMiB': 256,
                'Backend': 'polystrata',
                'SpeculativeMode': 'mtp',
                'RetainedSessions': 2,
                'WorkerArgs': ['--elastic-experts-headroom',
                               '--elastic-headroom-mib',
                               '128',
                               '--elastic-physical-reserve-mib',
                               '512']}}
STRINGS = {'Tokenizer','PolystrataSource','Python','VisionWorker','Mmproj','QueryScorer','Backend', 'Model', 'Worker', 'Gpu', 'ListenHost', 'KvType', 'KvTypeK', 'KvTypeV', 'DiskPath',
           'VisionResidency',
           'DeviceProfile', 'ScoreAggregation', 'QueryScorer', 'ApiKey', 'ModelAlias', 'LogLevel',
           'ChatTemplateFile', 'ThinkingMode', 'ReasoningEffort', 'SpeculativeMode', 'DraftPolicy', 'DraftModel'}
INTEGERS = {'BlockTokens', 'VramReserveMiB', 'Context', 'Budget', 'Reserve', 'HostMiB', 'Prefill', 'MaxTokens', 'Port',
            'MtpDrafts', 'NgramDrafts', 'NgramMinMatch', 'Concurrency', 'RetainedSessions',
            'VisionTokens', 'ThinkingBudget', 'SinkTokens', 'RecentTokens', 'DiskMiB', 'DraftTokens',
            'QueryMaxTokens', 'QueryReplayMaxTokens', 'ReplayMaxTokens'}
BOOLEANS = {'AdaptiveMtp', 'FastPrefill', 'Vision', 'NoLrfu', 'Lrfu', 'PreserveThinking'}
DECIMALS = {'LrfuLambda', 'LrfuTopFraction', 'GraphAlpha'}
# Deliberately limited native escape hatch. Common options must not be duplicated.
NATIVE = {'llamacpp': {'--threads', '--threads-batch', '--n-gpu-layers'},
          'ninfer': {'--threads'}, 'polystrata': {'--threads', '--elastic-headroom-mib', '--elastic-physical-reserve-mib'}}
NATIVE_SWITCHES = {'polystrata': {'--elastic-experts', '--elastic-experts-headroom'}}


def without_secrets(data):
    clean = copy.deepcopy(data)
    clean.get('parameters', {}).pop('ApiKey', None)
    return clean


def normalize(data, backends):
    if (not isinstance(data, dict) or type(data.get('schema_version')) is not int
            or data.get('schema_version') != 1 or set(data) != {'schema_version', 'parameters'}):
        raise ValueError('Unsupported configuration schema')
    params = data.get('parameters')
    if not isinstance(params, dict):
        raise ValueError('parameters must be an object')
    allowed = STRINGS | INTEGERS | BOOLEANS | DECIMALS | {'StageLayers', 'WorkerArgs'}
    if set(params) - allowed:
        raise ValueError('Unknown parameters: ' + ', '.join(sorted(set(params) - allowed)))
    p = copy.deepcopy(params)
    if p.get('Backend') not in NATIVE or p.get('Backend') not in backends:
        raise ValueError('This backend is not installed in the selected backend checkout')
    if p['Backend'] == 'polystrata':
        p.setdefault('BlockTokens', 64)
        if type(p['BlockTokens']) is not int or p['BlockTokens'] not in (32, 64):
            raise ValueError('PolyStrata BlockTokens must be 32 or 64')
    elif 'BlockTokens' in p:
        raise ValueError('BlockTokens configures PolyStrata in this console')
    for key, value in p.items():
        if key in STRINGS and (not isinstance(value, str) or '\x00' in value or len(value) > 8192):
            raise ValueError(f'{key} must be a string without NUL characters')
        if key in INTEGERS and (type(value) is not int or not 0 <= value <= 2147483647):
            raise ValueError(f'{key} must be a nonnegative integer')
        if key in BOOLEANS and type(value) is not bool:
            raise ValueError(f'{key} must be boolean')
        if key in DECIMALS and (type(value) not in (int, float) or not 0 <= value <= 1):
            raise ValueError(f'{key} must be between 0 and 1')
    # Old signed-cosine templates predate QueryScorer. Resolve their intent once
    # and submit canonical independent controls, including explicit false.
    legacy_scoring = 'QueryScorer' not in p and bool({'NoLrfu', 'LrfuLambda', 'LrfuTopFraction'} & p.keys())
    if legacy_scoring:
        p['QueryScorer'] = 'meanq-cosine' if (
            not p.get('NoLrfu', False) or p.get('ScoreAggregation') == 'max' or
            'LrfuLambda' in p or 'LrfuTopFraction' in p) else 'page-softmax'
    if 'NoLrfu' in p:
        if 'Lrfu' in p and p['Lrfu'] == p['NoLrfu']:
            raise ValueError('Lrfu conflicts with NoLrfu')
        p['Lrfu'] = not p.pop('NoLrfu')
        if legacy_scoring and not p['Lrfu']:
            p.pop('LrfuLambda', None); p.pop('LrfuTopFraction', None)
    if p.get('QueryScorer', 'page-softmax') not in {'page-softmax', 'meanq-cosine'}:
        raise ValueError('Unsupported QueryScorer')
    if p.get('ScoreAggregation', 'mean') not in {'mean', 'max'}:
        raise ValueError('Unsupported ScoreAggregation')
    for key in ('QueryMaxTokens', 'QueryReplayMaxTokens'):
        if key in p and p[key] <= 0:
            raise ValueError(f'{key} must be positive')
    history = p.get('Lrfu', p.get('QueryScorer') == 'meanq-cosine')
    if not history and {'LrfuLambda', 'LrfuTopFraction'} & p.keys():
        raise ValueError('LRFU tuning requires enabled LRFU')
    if 'LrfuTopFraction' in p and p['LrfuTopFraction'] <= 0:
        raise ValueError('LrfuTopFraction must be in (0,1]')
    if p.get('ListenHost', '127.0.0.1') not in {'127.0.0.1', '0.0.0.0'}:
        raise ValueError('ListenHost must be 127.0.0.1 or 0.0.0.0')
    if not 1 <= p.get('Port', 18200) <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    if p.get('ApiKey') and (len(p['ApiKey']) > 512 or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*', p['ApiKey'])):
        raise ValueError('ApiKey must be a single ASCII token (letters, digits, . _ ~ + / - and optional trailing =)')
    if p.get('LogLevel', '') not in {'', 'off', 'error', 'warning', 'info', 'trace', 'debug'}:
        raise ValueError('Unsupported LogLevel')
    if p.get('ThinkingMode', 'default') not in {'default', 'on', 'off'}:
        raise ValueError('ThinkingMode must be default, on or off')
    if p['Backend'] == 'ninfer' and p.get('ThinkingMode') == 'on':
        raise ValueError('NInfer supports default/off at startup; thinking-on is selected by requests')
    if p.get('ReasoningEffort') not in (None, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'):
        raise ValueError('Unsupported ReasoningEffort')
    if p.get('ThinkingMode') == 'off' and p.get('ReasoningEffort'):
        raise ValueError('ReasoningEffort conflicts with ThinkingMode off')
    if p['Backend'] != 'llamacpp' and {'KvTypeK', 'KvTypeV'} & p.keys():
        raise ValueError('K/V 独立格式仅适用于 llama.cpp；NInfer 请使用 KV 格式选项。')
    if p.get('Concurrency') is not None and not 1 <= p['Concurrency'] <= 8:
        raise ValueError('Concurrency must be 1..8')
    if p.get('ChatTemplateFile') and not Path(p['ChatTemplateFile']).is_file():
        raise ValueError('ChatTemplateFile must be an existing template file')
    if 'SpeculativeMode' in p or 'DraftTokens' in p or 'DraftPolicy' in p or 'DraftModel' in p:
        mode = p.get('SpeculativeMode', 'mtp' if p.get('MtpDrafts', 0) else 'none')
        profile = SPECULATIVE_PROFILES[p['Backend']].get(mode)
        if profile is None:
            raise ValueError('Unsupported SpeculativeMode')
        count = p.get('DraftTokens', p.get('MtpDrafts', 5 if mode == 'dflash2' else 4 if mode == 'mtp' else 0))
        policy = p.get('DraftPolicy', 'adaptive' if p.get('AdaptiveMtp') else 'fixed')
        if policy not in {'fixed', 'adaptive'}:
            raise ValueError('DraftPolicy must be fixed or adaptive')
        if 'DraftTokens' in p and 'MtpDrafts' in p and count != p['MtpDrafts']:
            raise ValueError('DraftTokens conflicts with MtpDrafts')
        if 'DraftPolicy' in p and 'AdaptiveMtp' in p and (policy == 'adaptive') != p['AdaptiveMtp']:
            raise ValueError('DraftPolicy conflicts with AdaptiveMtp')
        if p['Backend'] == 'polystrata' and mode == 'mtp':
            policy = 'adaptive'
            p['DraftPolicy'] = policy
            p.pop('AdaptiveMtp', None)
        if mode == 'none':
            if count or policy == 'adaptive' or p.get('NgramDrafts') or p.get('DraftModel'):
                raise ValueError('Disabled speculative decoding cannot have active draft options')
        else:
            if not 1 <= count <= profile['max_draft_tokens']:
                raise ValueError('DraftTokens exceeds this backend/algorithm capability')
            if policy == 'adaptive' and not profile['adaptive']:
                raise ValueError('Adaptive drafts are unavailable for this backend/algorithm')
            if p.get('NgramDrafts') and not profile['ngram']:
                raise ValueError('Ngram drafts are unavailable for this backend/algorithm')
            if p.get('Concurrency', 1) > profile['max_concurrency']:
                raise ValueError('This speculative algorithm does not support the selected concurrency')
            if len(p.get('Gpu', '').split(',')) > profile['max_devices']:
                raise ValueError('This speculative algorithm does not support the selected GPU count')
            draft = p.get('DraftModel', '')
            if profile['draft_asset'] == 'embedded' and draft:
                raise ValueError('NInfer uses an embedded draft component')
            if profile['draft_asset'] == 'required_gguf' and not draft:
                raise ValueError('DFlash2 requires DraftModel with a compatible GGUF')
            if draft and (Path(draft).suffix.lower() != '.gguf' or not Path(draft).is_file()):
                raise ValueError('DraftModel must be an existing GGUF file')
    if p['Backend']=='polystrata':
        if p.get('KvType','f16') not in {'f16','q8_0'}: raise ValueError('PolyStrata KV format must be f16 or q8_0')
        if ',' in p.get('Gpu',''): raise ValueError('PolyStrata currently supports one GPU')
        if p.get('ReasoningEffort') not in (None,'none','low','medium','high','xhigh'): raise ValueError('PolyStrata reasoning effort must be none, low, medium or high')
        if p.get('ThinkingMode')=='on' and p.get('ReasoningEffort')=='none': raise ValueError('ThinkingMode conflicts with ReasoningEffort none')
    gpu = p.get('Gpu', '')
    if gpu and not all(re.fullmatch(r'GPU-[0-9a-fA-F-]{8,64}', x) for x in gpu.split(',')):
        raise ValueError('Select GPUs by UUID, separated by commas')
    layers = p.get('StageLayers', [])
    if not isinstance(layers, list) or len(layers) > 8 or any(type(x) is not int or x < 1 for x in layers):
        raise ValueError('StageLayers must contain positive integers')
    args = p.get('WorkerArgs', [])
    if not isinstance(args, list) or len(args) > 20 or any(not isinstance(x, str) for x in args):
        raise ValueError('Native arguments must be an array of strings')
    seen = set()
    index = 0
    while index < len(args):
        flag = args[index]
        if flag in seen:
            raise ValueError(f'Unsupported or duplicate native option: {flag}')
        seen.add(flag)
        index += 1
        if flag in NATIVE_SWITCHES.get(p['Backend'], set()):
            continue
        if (flag not in NATIVE[p['Backend']] or index == len(args)
                or not re.fullmatch(r'\d{1,6}', args[index])):
            raise ValueError(f'Unsupported or incomplete native option: {flag}')
        if flag == '--elastic-headroom-mib' and not 0 <= int(args[index]) <= 1024:
            raise ValueError('Elastic headroom must be 0..1024 MiB')
        if flag == '--elastic-physical-reserve-mib' and not 256 <= int(args[index]) <= 4096:
            raise ValueError('Elastic physical reserve must be 256..4096 MiB')
        index += 1
    return {'schema_version': 1, 'parameters': p}


class WindowsJob:
    """Keep wrappers and their workers in an owned, kill-on-close process tree."""
    def __init__(self):
        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ('ReadOperationCount', 'WriteOperationCount',
                        'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
                      'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
                      'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
                      'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
                      'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL)}
        for name, (argtypes, restype) in signatures.items():
            fn = getattr(self.kernel, name); fn.argtypes = argtypes; fn.restype = restype
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED(); limits.BasicLimitInformation.LimitFlags = 0x2000
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close(); raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill(); process.wait()
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class Controller:
    def __init__(self, backend_root, state_dir, port):
        self.backend_root = Path(backend_root).resolve()
        self.state_dir = Path(state_dir).resolve(); self.state_dir.mkdir(parents=True, exist_ok=True)
        source = (self.backend_root / 'scripts/windows/start-backend.ps1').read_text(encoding='utf-8-sig')
        match = re.search(r"\[ValidateSet\(([^\n]+)\)\]\[string\]\$Backend", source)
        if not match:
            raise ValueError('Cannot detect supported backends from start-backend.ps1')
        self.backends = [name for name in re.findall(r"'([^']+)'", match[1]) if name in NATIVE]
        build_info=self.backend_root/'BUILD-INFO.json'
        if build_info.is_file():
            package=json.loads(build_info.read_text(encoding='utf-8-sig'))
            if package.get('backend')=='polystrata': self.backends=[name for name in self.backends if name=='polystrata']
        self.launcher_query_contract = all(name in source for name in ('QueryScorer', 'GraphAlpha', 'QueryMaxTokens', 'QueryReplayMaxTokens'))
        profile_file = self.backend_root / 'scripts/speculative-capabilities.json'
        self.speculative_profiles = json.loads(profile_file.read_text(encoding='utf-8-sig')) if profile_file.is_file() else {}
        if not self.backends:
            raise ValueError('This console requires a NInfer or llama.cpp launcher')
        self.port, self.token = port, secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.inspection_slots = threading.BoundedSemaphore(2)
        self.picker_lock = threading.Lock()
        self.process = self.job = self.log = None
        self.active = None
        self.active_plan = None
        self.worker_cache = {}
        self.observed_scoring = {}
        self.observed_offset = 0
        self.observed_pending = ''
        self.instance = secrets.token_hex(12)
        self.chat = ChatBridge(self)
        self.redactions = set()
        self.exit_code = None
        self.draft_path = self.state_dir / 'launch-draft.json'
        self.log_path = self.state_dir / 'service.log'
        # This describes the file, not the editing draft or the live process.
        # Keep it after stop so the final output keeps its original identity.
        self.log_session = secrets.token_hex(12)
        self.log_metadata = {}
        manifest_path = ROOT / 'console-build.json'
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
            if manifest.get('launch_contract_version') != 2:
                raise ValueError('控制台组件版本不匹配，请完整更新启动包。 Console components do not match; update the complete package.')
            for name, digest in manifest.get('controller_files', {}).items():
                path = (ROOT / name).resolve()
                if not path.is_relative_to(ROOT.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError('控制台文件不匹配，请完整更新启动包。 Console file mismatch: ' + name)

    def worker_capabilities(self, worker, backend):
        path = Path(worker).resolve()
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns, backend)
        if key in self.worker_cache:
            return copy.deepcopy(self.worker_cache[key])
        try:
            run = subprocess.run([str(path), '--kvmem-capabilities'], capture_output=True,
                                 encoding='utf-8', errors='replace', timeout=10, cwd=path.parent,
                                 env=dict(os.environ, CUDA_VISIBLE_DEVICES='-1'),
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            value = json.loads(run.stdout)
            if run.returncode or not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('backend') != backend or value.get('query_scoring_revision', 0) < 2 or not value.get('independent_graph_lrfu') or not value.get('signed_lrfu_hits'):
                raise ValueError('incompatible worker contract')
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise ValueError('后端版本不支持当前启动配置，请完整更新后端和控制台。 '
                             'Worker does not support this launch contract; update the complete package. ' + str(path)) from error
        with path.open('rb') as stream:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                hasher.update(chunk)
            digest = hasher.hexdigest()
        value['executable_sha256'] = digest
        value['path'] = str(path)
        self.worker_cache = {key: value}
        return copy.deepcopy(value)

    @staticmethod
    def scoring_from_argv(argv):
        def value(flag, default):
            return argv[argv.index(flag) + 1] if flag in argv else default
        return {'scorer': value('--kvmem-query-scorer', 'page-softmax'),
                'agg': value('--kvmem-score-agg', 'mean'), 'lrfu': '--kvmem-lrfu' in argv,
                'lambda': float(value('--kvmem-lrfu-lambda', .5)),
                'top_frac': float(value('--kvmem-lrfu-top-frac', .9)),
                'graph_alpha': float(value('--kvmem-graph-alpha', .2)),
                'query_max_tokens': int(value('--kvmem-query-max-tokens', 512)),
                'query_replay_max_tokens': int(value('--kvmem-query-replay-max-tokens', 2048))}

    def observe_scoring(self):
        if not self.active_plan or not self.log_path.is_file():
            return
        with self.log_path.open('rb') as stream:
            stream.seek(self.observed_offset)
            data = stream.read(1 << 20)
            self.observed_offset = stream.tell()
        text = self.observed_pending + data.decode('utf-8', errors='replace')
        lines = text.split('\n')
        self.observed_pending = lines.pop()
        for line in lines:
            for marker in ('KVMEM_SCORING ', 'KVMEM_LIMITS ', 'KVMEM_BLOCKS '):
                if marker not in line:
                    continue
                for key, value in re.findall(r'(\w+)=([^\s]+)', line.split(marker, 1)[1]):
                    if key in {'scorer', 'agg'}:
                        self.observed_scoring[key] = value
                    elif key == 'lrfu':
                        self.observed_scoring[key] = value == '1'
                    elif key in {'lambda', 'top_frac', 'graph_alpha'}:
                        self.observed_scoring[key] = float(value)
                    elif key in {'query_max_tokens', 'query_replay_max_tokens', 'block_tokens'}:
                        self.observed_scoring[key] = int(value)

    def worker_defaults(self):
        # A source checkout and a runnable installation need not share a directory.
        # Explicit registrations remain authoritative, including missing paths.
        registration = self.state_dir / 'worker-paths.json'
        paths = json.loads(registration.read_text(encoding='utf-8-sig')) if registration.exists() else {}
        if (not isinstance(paths, dict) or set(paths) - set(self.backends)
                or any(not isinstance(p, str) or not Path(p).is_absolute() or '\x00' in p
                       for p in paths.values())):
            raise ValueError('Invalid worker-paths.json: map backend names to absolute executable paths')
        names = {'ninfer': ('ninfer-kvmem-server.exe', 'ninfer-serve.exe'),
                 'llamacpp': ('llama-kvmem-server.exe',), 'polystrata':('strata-poly.exe',)}
        result = {}
        for backend in self.backends:
            if backend in paths:
                path = Path(paths[backend])
            else:
                # Search only known names in bounded installation locations; never
                # execute discovered files, search PATH, or recurse into other builds.
                roots = dict.fromkeys((self.backend_root, ROOT.resolve()))
                candidates = [directory / name
                              for root in roots
                              for directory in (root, root / 'bin', root / 'workers' / backend)
                              for name in names[backend]]
                fallback = self.backend_root / 'workers' / backend / names[backend][0]
                path = next((candidate for candidate in candidates if candidate.is_file()), fallback)
            result[backend] = {'path': str(path), 'available': path.is_file()}
        return result

    def draft(self):
        if self.draft_path.exists():
            return without_secrets(json.loads(self.draft_path.read_text(encoding='utf-8')))
        backend = DEFAULTS['Backend'] if DEFAULTS['Backend'] in self.backends else self.backends[0]
        return {'schema_version': 1, 'parameters': {**DEFAULTS, **copy.deepcopy(ENGINE_DEFAULTS[backend])}}

    def save(self, data):
        clean = without_secrets(normalize(data, self.backends))
        with self.lock:
            temp = self.draft_path.with_suffix('.tmp')
            temp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self.draft_path)
        return clean

    def redact(self, text):
        for secret in sorted(self.redactions, key=len, reverse=True):
            text = text.replace(secret, '<redacted>')
        return text

    def plan(self, data, *, reveal_secrets=False):
        clean = normalize(data, self.backends)
        if not self.launcher_query_contract:
            raise ValueError('启动脚本版本不匹配，请完整更新控制台和启动脚本。 Launcher version mismatch; update the complete console package.')
        api_key = clean['parameters'].pop('ApiKey', '')
        if api_key:
            self.redactions.add(api_key)
        if os.name != 'nt':
            raise ValueError('Service launch currently requires Windows PowerShell')
        params = clean['parameters']
        if params.get('Port', 18200) == self.port:
            raise ValueError('Inference service and console must use different ports')
        params = copy.deepcopy(params)
        if not params.get('Worker'):
            worker = self.worker_defaults()[params['Backend']]
            if not worker['available']:
                raise ValueError('未找到可用的后端程序。请在“引擎专属参数”的“后端程序”中填写可执行文件路径，'
                                 '或在控制台 worker-paths.json 中登记安装位置。')
            params['Worker'] = worker['path']
        elif not Path(params['Worker']).is_file():
            raise ValueError('指定的后端程序不存在，请检查“引擎专属参数 → 后端程序”：' + params['Worker'])
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', encoding='utf-8',
                                         dir=self.state_dir, delete=False) as stream:
            json.dump(params, stream, ensure_ascii=False); name = stream.name
        try:
            powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            run = subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                  '-File', str(ROOT / 'scripts/windows/describe-launch-json.ps1'),
                                  '-BackendRoot', str(self.backend_root), '-ConfigFile', name],
                                 capture_output=True, encoding='utf-8', errors='replace', timeout=45,
                                 cwd=self.backend_root,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            if run.returncode:
                message = run.stderr.strip()[-6000:] or run.stdout.strip()[-6000:]
                concise = re.search(r'\[KVMEM_CONFIG\]\s*([^\r\n]+)', message)
                raise ValueError(self.redact(concise.group(1).strip() if concise else message))
            # PowerShell warnings may precede the JSON result.
            start = run.stdout.find('{')
            result = json.loads(run.stdout[start:])
            argv = result.get('argv')
            if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or '\x00' in x for x in argv):
                raise ValueError('Launcher returned invalid argv')
            if api_key:
                if params['Backend']=='polystrata':
                    result.setdefault('environment',{})['STRATA_API_KEY']=api_key if reveal_secrets else '<redacted>'
                else:
                    argv.extend(['--api-key', api_key if reveal_secrets else '<redacted>'])
            result['warnings'] = self.redact(run.stdout[:start].strip())
            native_argv = result.get('native_argv', argv)
            result['worker'] = self.worker_capabilities(native_argv[0], params['Backend'])
            result['resolved_scoring'] = self.scoring_from_argv(native_argv)
            if params['Backend'] == 'polystrata':
                block = params.get('BlockTokens', 64)
                if block not in result['worker'].get('block_tokens', {}).get('supported', []):
                    raise ValueError('PolyStrata worker does not support BlockTokens; update the backend')
                if '--kvmem-block-tokens' not in native_argv or int(native_argv[native_argv.index('--kvmem-block-tokens') + 1]) != block:
                    raise ValueError('PolyStrata BlockTokens differs from the native launch plan')
                result['resolved_scoring']['block_tokens'] = block
            return result
        finally:
            Path(name).unlink(missing_ok=True)

    def browse_model(self, data, folder=False, template=False):
        if not isinstance(data, dict) or set(data) - {'backend', 'current_path'}:
            raise ValueError('Expected backend and optional current_path')
        backend, current = data.get('backend'), data.get('current_path', '')
        if backend not in self.backends or not isinstance(current, str) or '\x00' in current or len(current) > 8192:
            raise ValueError('Invalid model picker request')
        if os.name != 'nt':
            raise ValueError('The native model picker currently requires Windows')
        picker = ROOT / 'scripts/windows/select-model.ps1'
        if not picker.is_file():
            raise ValueError('选择窗口组件缺失，请更新完整的控制台包；也可以直接粘贴模型文件或目录的完整路径。')
        if not self.picker_lock.acquire(blocking=False):
            raise ValueError('已有一个系统选择窗口，请先完成或关闭它。')
        try:
            try:
                remembered = self._model_directory_paths() if not current else []
            except ValueError:
                remembered = []
            initial = (Path(current.strip().strip('"')).expanduser() if current else
                       next((path for path in remembered if path.is_dir()), self.backend_root / 'models'))
            if not initial.is_dir():
                initial = initial.parent
            while not initial.is_dir() and initial.parent != initial:
                initial = initial.parent
            powershell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            result = subprocess.run([powershell, '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(picker),
                                     '-Backend', backend, '-InitialDirectory', str(initial.resolve())] + (['-Folder'] if folder else ['-Template'] if template else []),
                                    capture_output=True, encoding='utf-8', errors='replace', timeout=300,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                # Startup errors occur before the helper can set UTF-8. Raw PowerShell
                # diagnostics may use a different encoding and do not belong in the UI.
                raise ValueError('无法打开系统选择窗口。请重试，或直接粘贴模型文件或目录的完整路径。')
            try:
                selected = json.loads(result.stdout).get('path')
            except (ValueError, AttributeError):
                raise ValueError('选择窗口返回异常，请重试或直接粘贴完整路径。') from None
            if selected is None:
                return {'path': None}
            if not isinstance(selected, str):
                raise ValueError('Invalid model selection result')
            path = Path(selected).resolve(strict=True)
            if folder:
                if not path.is_dir():
                    raise ValueError('Select a model directory')
                return {'path': str(path)}
            suffix = '.ninfer' if backend == 'ninfer' else '.gguf'
            if not path.is_file() or (not template and path.suffix.lower() != suffix):
                raise ValueError(f'{backend} requires a {suffix} model file')
            return {'path': str(path), 'name': path.name}
        except subprocess.TimeoutExpired:
            raise ValueError('选择窗口已超时，请重新打开，或直接粘贴完整路径。') from None
        except OSError:
            raise ValueError('无法打开系统选择窗口，请直接粘贴完整路径。') from None
        finally:
            self.picker_lock.release()

    @staticmethod
    def model_directory(value, must_exist=True):
        if not isinstance(value, str) or '\x00' in value or not 0 < len(value) <= 8192:
            raise ValueError('请输入模型文件夹的完整路径。')
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError('请输入完整的文件夹路径，例如 D:\\Models。')
        try:
            path = path.resolve() if must_exist else Path(os.path.abspath(path))
            if path == Path(path.anchor) or str(path).startswith('\\\\'):
                raise ValueError('请选择本地的模型文件夹，例如 D:\\Models；暂不扫描整个盘符或网络目录。')
            if must_exist and not path.is_dir():
                raise ValueError('文件夹不存在或无法访问，请检查路径：' + str(path))
        except OSError:
            raise ValueError('无法访问该文件夹，请检查路径和访问权限。') from None
        return path

    def _model_directory_paths(self):
        path = self.state_dir / 'model-directories.json'
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if (not isinstance(data, dict) or data.get('schema_version') != 1
                    or not isinstance(data.get('directories'), list) or len(data['directories']) > 16):
                raise ValueError()
            return list(dict.fromkeys(self.model_directory(item, must_exist=False)
                                      for item in data['directories']))
        except (OSError, ValueError, TypeError):
            raise ValueError('保存的模型目录记录无法读取；仍可直接选择模型文件。') from None

    def model_directories(self):
        with self.lock:
            paths = self._model_directory_paths()
        # Filesystem availability checks must not block owned-process status/control.
        directories = []
        for path in paths:
            try:
                available = path.is_dir()
            except OSError:
                available = False
            directories.append({'path': str(path), 'available': available})
        return {'directories': directories}

    def update_model_directories(self, data):
        if (not isinstance(data, dict) or set(data) != {'action', 'path'}
                or data['action'] not in ('add', 'remove')):
            raise ValueError('Invalid model directory request')
        path = self.model_directory(data['path'], must_exist=data['action'] == 'add')
        with self.lock:
            paths = [item for item in self._model_directory_paths() if item != path]
            if data['action'] == 'add':
                if len(paths) >= 16:
                    raise ValueError('最多记住 16 个模型目录，请先移除不再使用的目录记录。')
                paths.insert(0, path)
            target = self.state_dir / 'model-directories.json'
            temp = target.with_suffix('.tmp')
            try:
                temp.write_text(json.dumps({'schema_version': 1, 'directories': list(map(str, paths))},
                                           ensure_ascii=False, indent=2), encoding='utf-8')
                temp.replace(target)
            except OSError:
                raise ValueError('无法保存模型目录，请检查控制台状态目录的写入权限。') from None
        return self.model_directories()

    def scan_models(self, data):
        if not isinstance(data, dict) or set(data) - {'backend', 'current_path', 'directory', 'worker_path'}:
            raise ValueError('Invalid model scan request')
        backend = data.get('backend')
        if backend not in self.backends:
            raise ValueError('Unknown backend')
        for key in ('current_path', 'directory', 'worker_path'):
            value = data.get(key, '')
            if not isinstance(value, str) or '\x00' in value or len(value) > 8192:
                raise ValueError('Invalid search path')
        requested = []
        if data.get('directory'):
            folder = self.model_directory(data['directory'])
            self.update_model_directories({'action': 'add', 'path': str(folder)})
            requested.append((folder, True))
        else:
            if data.get('current_path'):
                current = Path(data['current_path'].strip().strip('"')).expanduser()
                if current.is_absolute():
                    requested.append((current if current.is_dir() else current.parent, True))
            requested.extend((folder, True) for folder in self._model_directory_paths())
            bases = list(dict.fromkeys((self.backend_root, ROOT.resolve())))
            worker = data.get('worker_path')
            if not worker:
                try:
                    worker = self.worker_defaults()[backend]['path']
                except ValueError:
                    worker = ''  # Model discovery remains usable before worker configuration is valid.
            if worker and Path(worker).is_absolute() and Path(worker).is_file():
                bases.append(Path(worker).parent)
            for base in dict.fromkeys(bases):
                # Models often sit beside the EXE. Inspect only that directory's files;
                # recurse into named model libraries, never entire build/workspace trees.
                requested.append((base, False))
                for parent in (base, *list(base.parents)[:4]):
                    for name in ('models', 'model'):
                        candidate = parent / name
                        if candidate.is_dir():
                            requested.append((candidate, True))
        roots = {}
        for folder, recursive in requested:
            folder = folder.resolve()
            if (folder == Path(folder.anchor) or str(folder).startswith('\\\\')
                    or not folder.is_absolute()):
                continue
            roots[folder] = roots.get(folder, False) or recursive
        locations = [{'path': str(folder), 'recursive': recursive, 'status': 'pending'}
                     for folder, recursive in roots.items()]
        suffix = '.ninfer' if backend == 'ninfer' else '.gguf'
        found, visited, candidates = {}, {}, 0
        other = {'.ninfer': set(), '.gguf': set()}
        reasons = set()
        skipped, limited = 0, False
        deadline = time.monotonic() + 3
        stack = [(folder, 6 if recursive else 0, index)
                 for index, (folder, recursive) in reversed(list(enumerate(roots.items())))]
        while stack:
            if time.monotonic() >= deadline or candidates >= 10000 or len(found) >= 200:
                reasons.add('time' if time.monotonic() >= deadline else 'entries' if candidates >= 10000 else 'results')
                limited = True; break
            folder, remaining, location = stack.pop()
            identity = os.path.normcase(str(folder))
            if visited.get(identity, -1) >= remaining:
                if locations[location]['status'] == 'pending':
                    locations[location]['status'] = 'covered'
                continue
            visited[identity] = remaining
            try:
                with os.scandir(folder) as entries:
                    if locations[location]['status'] == 'pending':
                        locations[location]['status'] = 'scanned'
                    for entry in entries:
                        candidates += 1
                        if candidates > 10000 or time.monotonic() >= deadline or len(found) >= 200:
                            reasons.add('time' if time.monotonic() >= deadline else 'entries' if candidates > 10000 else 'results')
                            locations[location]['status'] = 'partial'
                            limited = True; break
                        try:
                            stat = entry.stat(follow_symlinks=False)
                            # Junctions and links can lead outside the chosen folder or into loops.
                            if entry.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.startswith('.') or entry.name.lower() in ('node_modules', '__pycache__'):
                                    continue
                                if remaining:
                                    stack.append((Path(entry.path), remaining - 1, location))
                                elif locations[location]['recursive']:
                                    limited = True; reasons.add('depth')
                                    locations[location]['status'] = 'partial'
                            elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in other:
                                # Auxiliary vision projectors and non-first GGUF shards aren't standalone models.
                                if re.search(r'(?:mmproj|projector)', entry.name, re.I):
                                    continue
                                shard = re.search(r'-(\d{5})-of-\d{5}\.gguf$', entry.name, re.I)
                                if shard and shard[1] != '00001':
                                    continue
                                # Windows DirEntry.stat may omit the file ID. Read it explicitly
                                # so legacy hard links appear only once in the model library.
                                file_stat = os.stat(entry.path, follow_symlinks=False)
                                key = ((file_stat.st_dev, file_stat.st_ino) if file_stat.st_ino
                                       else os.path.normcase(entry.path))
                                actual_suffix = Path(entry.name).suffix.lower()
                                if actual_suffix != suffix:
                                    other[actual_suffix].add(key)
                                    continue
                                candidate = {'path': entry.path, 'name': entry.name,
                                             'bytes': file_stat.st_size, 'format': suffix[1:],
                                             'warnings': ['empty'] if file_stat.st_size == 0 else []}
                                def preference(item):
                                    path = Path(item['path'])
                                    canonical = (path.parent.name.casefold() == path.stem.casefold()
                                                 and path.parent.parent.name.casefold() == suffix[1:])
                                    return (not canonical, len(path.parts), item['path'].casefold())
                                if key not in found or preference(candidate) < preference(found[key]):
                                    found[key] = candidate
                        except OSError:
                            skipped += 1
                            locations[location]['status'] = 'partial'
            except OSError:
                skipped += 1
                locations[location]['status'] = 'unavailable' if folder in roots else 'partial'
        return {'models': sorted(found.values(), key=lambda x: (bool(x['warnings']), x['name'].casefold(), x['path'])),
                'roots': [str(folder) for folder in roots], 'locations': locations,
                'limited': limited, 'limit_reasons': sorted(reasons), 'skipped': skipped,
                'other_formats': {key[1:]: len(value) for key, value in other.items()}}

    def status(self):
        with self.lock:
            self.observe_scoring()
            running = self.process is not None and self.process.poll() is None
            if self.process and not running:
                self.exit_code = self.process.returncode
                self._release()
            result = {'state': 'starting' if running else ('exited' if self.exit_code is not None else 'stopped'),
                      'pid': self.process.pid if running else None, 'exit_code': self.exit_code, 'instance':self.instance,
                      'active_config': without_secrets(self.active) if self.active else None,
                      'api_key_configured': bool(self.active and self.active['parameters'].get('ApiKey')), 'url': None}
            result['planned_scoring'] = copy.deepcopy(self.active_plan.get('resolved_scoring')) if self.active_plan else None
            result['effective_scoring'] = dict(self.observed_scoring) or None
            result['worker'] = copy.deepcopy(self.active_plan.get('worker')) if self.active_plan else None
            planned = result['planned_scoring']
            result['scoring_confirmed'] = bool(planned and all(
                key in self.observed_scoring and self.observed_scoring[key] == value for key, value in planned.items()))
            result['scoring_mismatch'] = bool(planned and set(planned).issubset(self.observed_scoring)
                                              and not result['scoring_confirmed'])
            if running:
                port = self.active['parameters'].get('Port', 18200)
                result['url'] = f'http://127.0.0.1:{port}'
                try:
                    target = result['url'] + '/health'
                    api_key = self.active['parameters'].get('ApiKey')
                    if api_key:
                        target = urllib.request.Request(target, headers={'Authorization': 'Bearer ' + api_key})
                    with urllib.request.urlopen(target, timeout=0.5) as response:
                        if response.status == 200:
                            result['state'] = 'ready'
                except urllib.error.HTTPError as error:
                    error.close()
                except (OSError, urllib.error.URLError):
                    pass
            return result

    def _release(self):
        if self.job:
            self.job.close(); self.job = None
        if self.log:
            self.log.close(); self.log = None
        self.process = None

    def inspect_model(self, data, *, for_launch=False):
        parameters = request_parameters(data)
        if parameters['Backend'] not in self.backends:
            raise ValueError('Backend is not installed')
        if not parameters.get('Worker'):
            parameters['Worker'] = self.worker_defaults().get(parameters['Backend'], {}).get('path', '')
        acquired = self.inspection_slots.acquire(timeout=11) if for_launch else self.inspection_slots.acquire(blocking=False)
        if not acquired:
            if for_launch:
                raise ValueError('模型体检正在忙，请稍后重试启动 / Model inspection busy; retry launch shortly')
            return unchecked('Inspection is busy; retry shortly')
        try:
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/model_health.py')],
                                 input=json.dumps({'schema_version': 1, 'parameters': parameters}),
                                 capture_output=True, text=True, encoding='utf-8',
                                 cwd=self.backend_root, timeout=10,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            if run.returncode != 0 or len(run.stdout) > 262144:
                return unchecked('Inspector did not return a bounded report')
            report = json.loads(run.stdout)
            if not isinstance(report, dict) or report.get('schema_version') != 1 or type(report.get('can_start')) is not bool:
                return unchecked('Unsupported inspector response')
            return report
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return unchecked('Inspector unavailable or timed out; retry or attempt launch')
        finally:
            self.inspection_slots.release()

    def start(self, data):
        clean = normalize(data, self.backends)
        # Never trust a cached browser report. Inspect this exact launch request.
        health = self.inspect_model(clean, for_launch=True)
        if not health['can_start']:
            errors = [c['zh'] + ' / ' + c['en'] + ': ' + c.get('detail', '')
                      for c in health['checks'] if c['status'] == 'error']
            raise ValueError('Model inspection blocked launch: ' + '; '.join(errors))
        with self.lock:
            if self.process and self.process.poll() is None:
                raise ValueError('A managed service is already running; stop it before starting another')
            self._release()
            result = self.plan(clean, reveal_secrets=True)
            port = clean['parameters'].get('Port', 18200)
            with socket.socket() as probe:
                if os.name == 'nt':
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind((clean['parameters'].get('ListenHost', '127.0.0.1'), port))
                except OSError as error:
                    raise ValueError(f'端口 {port} 无法使用，可能已有服务占用。请在“服务与并发”中更换端口，'
                                     '或先停止原服务。此控制台不会停止外部服务。') from error
            env = dict(os.environ); env.update(result.get('environment', {}))
            plan_summary = {'resolved_scoring': result.get('resolved_scoring'), 'worker': result.get('worker')}
            self.observed_scoring = {}; self.observed_offset = 0; self.observed_pending = ''
            self.log = self.log_path.open('wb')
            self.log_session = secrets.token_hex(12)
            self.log_metadata = {'backend': clean['parameters']['Backend'],
                                 'model': Path(clean['parameters'].get('Model', '')).name,
                                 'started_at': int(time.time() * 1000)}
            self.job = WindowsJob()
            try:
                # The gate waits for argv before creating any descendants. Assigning
                # it to the job first closes the spawn-before-job-assignment race.
                self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker-gate'],
                                                cwd=self.backend_root, env=env,
                                                stdin=subprocess.PIPE, stdout=self.log, stderr=self.log,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                self.job.assign(self.process)
                self.process.stdin.write((json.dumps(result['argv']) + '\n').encode('utf-8'))
                self.process.stdin.close()
            except Exception:
                self._release(); raise
            self.active, self.active_plan, self.exit_code = clean, plan_summary, None
            self.instance = secrets.token_hex(12)
            return self.status()

    def stop(self):
        self.chat.cancel_all()
        with self.lock:
            process = self.process
            if self.job:
                self.job.close(); self.job = None
            if process:
                process.wait(timeout=10)
            self._release(); self.exit_code = None; self.active = None; self.active_plan = None
            self.observed_scoring = {}; self.observed_offset = 0; self.observed_pending = ''
            self.instance = secrets.token_hex(12)
            return self.status()

    def log_tail(self):
        if not self.log_path.exists():
            return ''
        with self.log_path.open('rb') as stream:
            stream.seek(0, 2); stream.seek(max(0, stream.tell() - 32768))
            return self.redact(stream.read().decode('utf-8', errors='replace'))

    def log_snapshot(self, revision=''):
        """Bounded read of the common stdout/stderr file; no inference/health calls.

        Return complete terminal lines while the process is writing. In addition
        to keeping UTF-8 intact this prevents exposing a partially written key
        before the normal redactor can match it. A stopped process's last line
        is useful even without a newline. No client-supplied path is accepted.
        """
        limit = 256 * 1024
        with self.lock:
            code = self.process.poll() if self.process else self.exit_code
            running = self.process is not None and code is None
            result = {'protocol': 1, 'session': self.log_session, **self.log_metadata,
                      'running': running, 'exit_code': code, 'limit_bytes': limit,
                      'available': False, 'bytes': 0, 'updated_at': None,
                      'truncated': False, 'pending': False, 'text': ''}
            try:
                stream = self.log_path.open('rb')
            except FileNotFoundError:
                result['revision'] = self.log_session + ':missing'
                return result
            with stream:
                stat = os.fstat(stream.fileno())
                size = stat.st_size
                stamp = f'{self.log_session}:{stat.st_ino}:{size}:{stat.st_mtime_ns}:{running}:{len(self.redactions)}'
                result.update(available=True, bytes=size, updated_at=stat.st_mtime_ns // 1_000_000,
                              revision=stamp, truncated=size > limit)
                # Metadata is returned even when the text revision is unchanged.
                if running and size:
                    stream.seek(size - 1)
                    result['pending'] = stream.read(1) not in (b'\r', b'\n')
                if revision == stamp:
                    result['text'] = None
                    return result
                start = max(0, size - limit)
                stream.seek(start)
                data = stream.read(size - start)
            if start:
                # Discard the first partial line (including partial UTF-8/ANSI).
                boundary = re.search(b'[\r\n]', data)
                data = data[boundary.end():] if boundary else b''
            if running and data and data[-1:] not in (b'\r', b'\n'):
                end = max(data.rfind(b'\n'), data.rfind(b'\r')) + 1
                data = data[:end]
                result['pending'] = True
            result['text'] = self.redact(data.decode('utf-8', errors='replace'))
            return result

    def monitor_authorization(self):
        with self.lock:
            key = self.active['parameters'].get('ApiKey') if self.active else None
            return 'Bearer ' + key if key else None


def gpu_inventory():
    try:
        run = subprocess.run(['nvidia-smi', '--query-gpu=uuid,name,memory.total', '--format=csv,noheader,nounits'],
                             capture_output=True, text=True, timeout=5,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return [dict(zip(('uuid', 'name', 'memory_mib'), (x.strip() for x in line.split(','))))
                for line in run.stdout.splitlines() if len(line.split(',')) == 3]
    except (OSError, subprocess.TimeoutExpired):
        return []


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        # Windows SO_REUSEADDR permits a second process to bind the same address.
        # Exclusive ownership is required for reliable repeated-click detection.
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def control(self):
        return self.server.control

    def reply(self, code, body, content_type='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            # Navigation/refresh can abort either headers or body on Windows.
            self.close_connection = True

    def trusted(self, mutate=False):
        host = self.headers.get('Host', '')
        hosts = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        origin = self.headers.get('Origin')
        if host not in hosts or (origin is not None and origin != 'http://' + host):
            self.reply(403, {'error': 'Untrusted host or origin'}); return False
        if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
            self.reply(403, {'error': 'Cross-site request rejected'}); return False
        if mutate and not secrets.compare_digest(self.headers.get('X-KVMem-Token', ''), self.control.token):
            self.reply(403, {'error': 'Invalid console token'}); return False
        return True

    def do_GET(self):
        if not self.trusted():
            return
        path = urlsplit(self.path).path
        try:
            if path == '/api/chat/context':
                self.reply(200, self.control.chat.context())
            elif path.startswith('/api/chat/requests/'):
                record = self.control.chat.record(path.rsplit('/',1)[1])
                self.reply(200 if record else 404, record or {'error':'Unknown request'})
            elif path.startswith('/chat/view/'):
                self.chat_asset(path[len('/chat/view/'):])
            elif path == '/api/launcher/info':
                self.reply(200, {'token': self.control.token, 'backends': self.control.backends,
                                'console_protocol': 1, 'console_root': str(ROOT.resolve()),
                                'model_library_version': 1,
                                'model_inspection_version': 1,
                                'kv_cache_version': 1,
                                'query_scoring_version': 2,
                                'speculative_capabilities': self.control.speculative_profiles,
                                'state_dir': str(self.control.state_dir),
                                'backend_root': str(self.control.backend_root), 'gpus': self.server.gpus,
                                'draft': self.control.draft(), 'status': self.control.status(),
                                'workers': self.control.worker_defaults(),
                                'native_options': {k: sorted(v | NATIVE_SWITCHES.get(k, set())) for k, v in NATIVE.items()}})
            elif path == '/api/launcher/model-directories':
                self.reply(200, self.control.model_directories())
            elif path == '/api/launcher/status':
                self.reply(200, self.control.status())
            elif path == '/api/launcher/hardware':
                self.reply(200, self.server.hardware.snapshot())
            elif path == '/api/launcher/log':
                self.reply(200, {'text': self.control.log_tail()})
            elif path == '/api/launcher/logs':
                revision = parse_qs(urlsplit(self.path).query).get('revision', [''])[0]
                self.reply(200, self.control.log_snapshot(revision))
            elif path in ('/api/monitor/snapshot', '/props', '/slots', '/stats'):
                state = self.control.status()
                if not state['url']:
                    self.reply(503, {'error': 'No managed service is running'}); return
                # Fixed read-only routes to the owned service, never an arbitrary URL.
                request = urllib.request.Request(state['url'] + path)
                authorization = self.control.monitor_authorization() or self.headers.get('Authorization')
                if authorization:
                    request.add_header('Authorization', authorization)
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        body = response.read(4 * 1024 * 1024 + 1)
                        if len(body) > 4 * 1024 * 1024:
                            raise ValueError('Monitor response too large')
                        self.reply(response.status, body)
                except urllib.error.HTTPError as error:
                    self.reply(error.code, {'error': 'Inference endpoint unavailable'})
                    error.close()
            elif path in ('/', '/monitor'):
                self.send_response(302)
                self.send_header('Location', '/monitor/')
                self.send_header('Content-Length', '0')
                self.end_headers()
            elif path in ('/monitor/', '/chat/'):
                self.asset('index.html')
            elif path.startswith('/monitor/') and '/' not in path[len('/monitor/'):]:
                self.asset(path[len('/monitor/'):])
            else:
                self.reply(404, {'error': 'Not found'})
        except (ValueError, OSError, urllib.error.URLError) as error:
            self.reply(503, {'error': str(error)})

    def asset(self, name):
        allowed = {'index.html', 'monitor.css', 'monitor.js', 'i18n.js', 'launcher.css', 'launcher.js',
                   'Strata-LICENSE.txt', 'Outfit-OFL.txt', 'outfit-latin-wght.woff2', 'console-shell.js', 'console-shell.css', 'history-export.html', 'logs.js', 'logs.css'}
        if name not in allowed:
            self.reply(404, {'error': 'Not found'}); return
        file = ROOT / 'ui/monitor' / name
        self.reply(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def chat_asset(self, name):
        roots = [ROOT/'ui/dist', self.control.backend_root/'workers/share/kvmem/ui', ROOT/'build/share/kvmem/ui']
        root = next((p for p in roots if (p/'index.html').is_file()), None)
        if root is None:
            self.reply(503, {'error':'Unified chat assets missing; rebuild the UI package'}); return
        root = root.resolve()
        file = (root/(name or 'index.html')).resolve()
        if root not in file.parents or not file.is_file() or file.suffix not in {'.html','.js','.css','.svg','.png','.ico','.woff','.woff2','.json'}:
            self.reply(404, {'error':'Not found'}); return
        self.reply(200,file.read_bytes(),mimetypes.guess_type(file.name)[0] or 'application/octet-stream')

    def do_POST(self):
        if not self.trusted(mutate=True):
            return
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Expected application/json')
            size = int(self.headers.get('Content-Length', '0'))
            is_chat = self.path == '/api/chat/completions'
            if not 0 < size <= (MAX_BODY if is_chat else 65536):
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(size))
            if is_chat:
                self.control.chat.stream(self, data); return
            if self.path == '/api/chat/cancel':
                self.reply(200,self.control.chat.cancel(str(data.get('id','')))); return
            actions = {'/api/launcher/save': self.control.save, '/api/launcher/preview': self.control.plan,
                       '/api/launcher/browse-model': self.control.browse_model,
                       '/api/launcher/browse-template': lambda data: self.control.browse_model(data, template=True),
                       '/api/launcher/browse-model-folder': lambda data: self.control.browse_model(data, folder=True),
                       '/api/launcher/scan-models': self.control.scan_models,
                       '/api/launcher/inspect-model': self.control.inspect_model,
                       '/api/launcher/model-directories': self.control.update_model_directories,
                       '/api/launcher/start': self.control.start, '/api/launcher/stop': lambda _: self.control.stop()}
            action = actions.get(self.path)
            if action is None:
                self.reply(404, {'error': 'Not found'}); return
            self.reply(200, action(data))
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            self.reply(400, {'error': self.control.redact(str(error))})


def open_console_browser(url):
    try:
        if webbrowser.open(url, new=2):
            return
    except (OSError, webbrowser.Error):
        pass
    print(f'Open this address in your browser: {url}', flush=True)


def matching_console(port, backend_root, state_dir):
    """Only reuse a controller from this installation and the same state directory."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://127.0.0.1:{port}/api/launcher/info', timeout=2) as response:
            body = response.read(262145)
            if len(body) > 262144:
                return False
            data = json.loads(body)
        if not isinstance(data, dict):
            return False
        return (data.get('console_protocol') == 1
                and Path(data['console_root']).resolve() == ROOT.resolve()
                and Path(data['backend_root']).resolve() == Path(backend_root).resolve()
                and Path(data['state_dir']).resolve() == Path(state_dir).resolve())
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return False


def choose_console_port(current_port, language='zh-CN'):
    english = language == 'en'
    if english:
        label = 'Default port' if current_port == 18199 else 'Port'
        print(f'{label} {current_port} is unavailable. It may be in use or reserved by Windows.', flush=True)
    else:
        label = '默认端口' if current_port == 18199 else '端口'
        print(f'{label} {current_port} 不可用，可能已被占用或被系统保留。', flush=True)
    prompt = ('Enter another port (1-65535, e.g. 18198; press Enter or type q to cancel): '
              if english else '请输入其他端口（1–65535，例如 18198；直接回车或输入 q 退出）：')
    cancelled = 'Startup cancelled.' if english else '已取消启动。'
    while True:
        try:
            value = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print('\n' + cancelled, flush=True)
            return None
        if not value or value.lower() in ('q', 'quit', 'exit'):
            print(cancelled, flush=True)
            return None
        try:
            port = int(value)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            print('Port must be an integer from 1 to 65535.' if english else '端口必须是 1 到 65535 之间的整数。', flush=True)
        elif port == current_port:
            print(f'Choose a port other than {current_port}.' if english else f'请选择与 {current_port} 不同的端口。', flush=True)
        else:
            return port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18199)
    parser.add_argument('--backend-root', type=Path, default=ROOT)
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'build/local-console')
    parser.add_argument('--open-browser', action='store_true', help='open the UI; reuse an identical running console')
    parser.add_argument('--prompt-port', action='store_true', help='ask for another console port when unavailable')
    parser.add_argument('--language', choices=('zh-CN', 'en'), default='zh-CN',
                        help='startup message language; en also opens the UI in English')
    args = parser.parse_args()
    control = Controller(args.backend_root, args.state_dir, args.port)
    while True:
        url = f'http://127.0.0.1:{args.port}/monitor/' + ('?lang=en' if args.language == 'en' else '')
        try:
            server = Server(('127.0.0.1', args.port), Handler)
            break
        except OSError as error:
            if (args.open_browser or args.prompt_port) and matching_console(args.port, args.backend_root, args.state_dir):
                print(f'KVMem console is already running: {url}', flush=True)
                if args.open_browser:
                    open_console_browser(url)
                return
            if not args.prompt_port:
                parser.exit(1, f'Cannot open console port {args.port}: {error}. '
                            'Close the other application or choose --port with a free port.\n')
            port = choose_console_port(args.port, args.language)
            if port is None:
                return
            args.port = port
    control.port = server.server_port
    server.control, server.gpus = control, gpu_inventory()
    server.hardware = SystemDiskSampler().start()
    url = f'http://127.0.0.1:{server.server_port}/monitor/' + ('?lang=en' if args.language == 'en' else '')
    print(f'KVMem console: {url}', flush=True)
    if args.language == 'en':
        print('\nHow to stop:\n'
              '  Keep this window open while using the console.\n'
              '  Close this window or press Ctrl+C to stop the console and its inference services, and unload their models.\n'
              '  Click "Stop service" in the UI to stop inference while keeping the console open.\n'
              '  Closing only the browser leaves services running.\n'
              '  Backends started separately are unaffected.\n', flush=True)
    else:
        print('\n使用说明：\n'
              '  保持此窗口开启，控制台才能持续运行。\n'
              '  关闭此窗口或按 Ctrl+C：结束控制台，并停止由它启动的推理服务、卸载模型。\n'
              '  页面点击“停止服务”：只停止推理，控制台继续保留。\n'
              '  只关闭浏览器：服务继续运行。\n'
              '  另外单独启动的后端服务不受影响。\n', flush=True)
    if args.open_browser:
        threading.Thread(target=open_console_browser, args=(url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.hardware.close()
        control.stop(); server.server_close()


if __name__ == '__main__':
    if sys.argv[1:] == ['--worker-gate']:
        argv = json.loads(sys.stdin.buffer.readline(65536))
        sys.exit(subprocess.call(argv, stdin=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW))
    else:
        main()
