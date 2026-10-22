# Graph recall C

Current source builds default to C for KVMem retrieval in both llama.cpp and NInfer; the scoring options below also expose Mean-Q cosine and normalized GQA max.
The resource recipe in `start-ninfer.ps1` still follows 0.18: context 204800,
B=36864, R=16384, host KV=12288 MiB, prefill=256, sink/recent=12288/1024,
adaptive MTP4 and ngram31/minimum12. Published 0.18 binaries retain their old
LRFU/max ranking. Rebuild both backend patches when using this source.

The implementation follows the C configuration of
[kvmem-vllm at 8560331](https://github.com/Di-Chai/kvmem-vllm/tree/8560331de4d9a889a1e884fe238b91e957a5958d).
This reference is an algorithm source; its model precision, batching and MTP
implementation are not copied into the two native engines.

## Query recall

For every actual query token, active full-attention layer and query head,
compute scaled dot products against the corresponding KV head's Mean-K pages,
then softmax over the eligible page domain. Average layers/heads and sum token
scores. Q and Mean-K have an explicit FP16 operand boundary. Recurrent layers
are absent, not zero-valued attention layers. This is the default mean-head
configuration; optional scoring variants are defined under Startup scoring options.

The denominator mask and graph transport mask are separate. The latter removes
the exact fixed set reserved by the picker, including mandatory media groups.
Mandatory blocks do not automatically disappear from the softmax denominator.
Sink/recent denominator masking follows the reference capacity conditions.
Both backends bound the query span to its last 512 tokens by default.
The native option is `--kvmem-query-max-tokens`.

After the initial score, perform one simultaneous propagation step with
alpha=0.2: at most 0.05 to each adjacent block and 0.1 through outgoing attention
edges. The remaining mass stays at the source. Missing local destinations keep
their mass at the source; surviving attention destinations are renormalized.
There is no second hop and no replacement of filtered top-four destinations.
Cross-message local edges receive a coefficient of 0.25; unknown boundaries
use 1. Message boundaries come from structured request/template metadata. Text
inside a user message cannot introduce a trusted boundary. Unsupported llama
templates and media use the unknown-boundary fallback.

An attention edge **source -> target** means sampled query tokens in the source
block attended to K tokens in the target block. All sampled rows of a source
are aggregated before selecting its four strongest past destinations. Self and
future edges are removed. A source row is sealed only once the complete source
block has been accepted. An unfinished source retains its raw accumulation.
Node identities are original logical block starts, never recycled native slots.

## Unified request and replay policy

Both backends distinguish new user Query recall from ordinary tool continuation.
Tool continuation (including a cold/restored session) appends normally and uses
resident-only Pressure when necessary. It does not import old Q to recall cold
history, probe tool text as Q, or replay the user-to-tool history. There is no
document-ingestion mode or new protocol field.

Query scoring binds the checkpoint immediately before the bounded Q: its exact
Mean-K boundary (including a partial page), graph and recoverable history domain.
The probe suffix is append workspace, not an additional history candidate domain.
Both backends share range/admission rules in `kvmem/query_policy.hpp`.

Continuation classification uses the verified input boundary, independently of
cached Q features. Q capture is enabled only for an admitted Query consumer;
tools, a proved complete resident view and refused replay do not copy/store
unused per-token Q. Mean-K and native attention feedback remain enabled.
NInfer includes native draft workspace and prior compaction in the resident
proof, so a prompt smaller than B+R can still require Query recall.

NInfer recycles at most two released checkpoint StateImage buffers per Program.
Live/aliased snapshots retain their own leases, each reused buffer is cleared
before capture, and idle capacity is included in `checkpoint_bytes`. The cache
reuses only existing buffers; it does not preallocate two additional snapshots.

`--kvmem-query-max-tokens` defaults to 512. Independently,
`--kvmem-query-replay-max-tokens` defaults to 2048 and bounds the complete causal
suffix from the actual recovery point, including template/media alignment.
Admission also checks selected prefix pages plus appended pages and native MTP
workspace against actual device capacity. Output length is not a replay budget.

Plan and admit before changing native selection or recurrent/MTP state. A proved
resident/unchanged view retains the first pass. A refused replay preserves its
complete first-pass view, state and logits. Admitted replay uses a fixed historical
selection: batching is allowed, Pressure during Query replay is forbidden. Mean-K
is rebuilt from the prefix and the probe's graph/feedback suffix is discarded;
replay advances accepted frontiers without publishing duplicate observations.
Failures/cancellation restore a verified checkpoint or invalidate the cache.

Quality comparisons use ordinary document-first requests followed by a separate
user question. Preserve the frozen original master; comparisons that share the new
workflow but replace the scorer must be labelled separately.

## Pressure and acceptance

`Query`, `PrefillPressure` and `DecodePressure` are explicit reasons. Only Query
can recall cold history or propagate through the graph. Pressure ranks the
accepted resident working set, including Query-selected old history and newly
generated blocks, by accumulated attention. Hard requirements stay mandatory.
Selection shrinks toward B and frees R for appending. R is not a generation
limit; the configured output/context limits still apply.

When there are N pending accepted output samples, pressure previews
`0.5 * previous_output_history + pending_mass / N`; with N=0, history is
unchanged. Preview neither commits nor decays stored history. If any output
score is positive across the candidate domain, output feedback is used;
otherwise prefill mass/exposure is used. This decision precedes fixed-set
filtering. Pressure never brings a cold block back into the resident domain.

Output samples occur every 16 accepted, actually consumed, non-thinking tokens.
Thinking delimiters are excluded. Prefill samples use interval 1024, stride32,
half-life8192 and the reference near-capacity condition (two chunks plus one
block of slack). Native Q is retained at the first/middle/last full-attention
layers, deduplicated for short layer slices. Page attention is token-log-sum-exp
followed by normalization across visible pages and head averaging. It retains
causal visibility and the actual native K representation/scales; it does not
apply softmax to Mean-K when measuring attention feedback.

Draft, rejected, replayed and cancelled suffixes do not publish reusable graph
or feedback contributions. A sampled emitted token only contributes when a
target forward has consumed it. Pending feedback, partial graph rows, parser
scope, accepted frontiers and sampling watermarks travel with checkpoints.
Restore validates these against the native accepted state before publication.

An NInfer reconstruction boundary inside an accepted MTP span first reads the
current verification K columns, then captures the temporary prefix. Returning
to the base restores graph, pending feedback and output sampling scope together
with native state and Mean-K, on success and on exceptions. The full span is
then committed once. Its temporary graph snapshot shares immutable chunks and
is charged to the existing statistics budget. llama.cpp complete sequence
removal resets the same conversation statistics as clear; internal Query replay
retains its explicit exemption from that full-history reset.

## Cost and ownership

- No full token-attention matrix is copied to the CPU. Native K stays in its
  backend layout. The new CUDA operations return only sampled page masses.
- NInfer reserves Q capture, logical maps, page LSE, normalizers and Query
  scratch in its startup memory plan. The Query scorer borrows that workspace;
  it does not allocate device buffers during a request. llama.cpp reuses a
  device-owned sampler/scorer workspace, with a generic GGML fallback.
- Query logits are tiled over at most 64 rows and at most 64 MiB of scratch.
  The llama attention sampler bounds its tile estimate at 32 MiB. Sampling
  reads native K, so its cost still grows with visible tokens and sample count.
  This first delivery adds kernels; reusing native attention reductions remains
  a possible later optimization.
- Graph and feedback statistics remain process-local RAM metadata even when
  native KV spills. Each conversation budget is 32 MiB and the parent budget
  is 256 MiB across active lanes and retained histories. Every live snapshot
  is charged its full logical footprint, even for shared immutable chunks;
  this deliberately may reject earlier than unique physical memory usage.
  Statistics are not part of the native KV payload quota. Budget failure is
  explicit; the engine does not silently revert to another ranking policy.
- Query fixed blocks include the complete overlapping media group. Pressure
  requires its mandatory set to be resident/incoming and cannot fetch cold KV
  to satisfy a ranking accident.

### Startup scoring options

Both native servers accept the same options. They affect Query scoring only;
Pressure, bounded replay, tool append, accepted-token accounting and rollback use
the shared execution policy described above. Options are fixed for the engine's
lifetime; histories are engine-local and llama lane transfer checks the scorer,
aggregation, graph alpha and LRFU configuration before attaching a conversation.

| Option | Values and default |
| --- | --- |
| `--kvmem-query-scorer` | `page-softmax` (default), `meanq-cosine` |
| `--kvmem-score-agg` | `mean`, `max`; omitted: page-softmax mean, meanq-cosine max |
| `--kvmem-lrfu` / `--no-kvmem-lrfu` | omitted: enabled only for meanq-cosine; explicit override wins regardless of scorer argument order |
| `--kvmem-lrfu-lambda` | [0,1], default 0.5; requires enabled LRFU |
| `--kvmem-lrfu-top-frac` | (0,1], default 0.9; requires enabled LRFU |
| `--kvmem-graph-alpha` | [0,1]; omitted: page-softmax 0.2, meanq-cosine 0; zero bypasses propagation |

Page-softmax max takes each head's page probabilities, the maximum within each
KV head's GQA group, then renormalizes that group over eligible pages. It averages
KV groups/layers and sums Query tokens. Mean-Q cosine first sums Query rows and
uses the existing cosine scorer: max compares individual Query heads; mean folds
heads within a GQA group before cosine. Its signed scores retain their ordering;
no clipping or offset enters LRFU. History is committed once at accepted prefill,
not on probes, tool continuations or speculative rejected rows.

Page-softmax plus LRFU and meanq-cosine plus nonzero graph alpha are rejected.
Selecting a scorer alone does not enable KVMem; use `--kvmem` or a positive budget.
Startup diagnostics print the resolved configuration.

```text
--kvmem-query-scorer meanq-cosine --kvmem-score-agg max --kvmem-lrfu
--kvmem-query-scorer meanq-cosine --kvmem-score-agg max --no-kvmem-lrfu
--kvmem-query-scorer page-softmax --kvmem-score-agg mean --kvmem-graph-alpha 0.2
--kvmem-query-scorer page-softmax --kvmem-score-agg max --kvmem-graph-alpha 0.2
```

Windows launchers expose `-QueryScorer`, `-ScoreAggregation`, `-GraphAlpha`,
`-Lrfu`/`-NoLrfu`, `-LrfuLambda` and `-LrfuTopFraction` with the same defaults.
The graphical launcher retains the default C recipe; these variants are CLI
options, not new UI controls. Published performance tables describe their
original release, not these variants.

## Focused qualification

Baseline: KVMem master `61569ff4bddcad60e8fad838e02f77a6190126d1` with its
pinned backend patches. Candidate and baseline use the same device/settings.
Windows, CUDA13.2, Release, sm120a; performance uses RTX5060Ti UUID
`GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c`. Correctness uses RTX5050 Laptop
UUID `GPU-14f08a8c-8d62-4338-8ae4-c669889cdb29`.

Core checks cover numerical softmax/GQA, more than 32768 pages, separate masks,
message-local edges, sealing/rollback, pending feedback, resident-only pressure,
fixed pickers and bounded snapshot ownership. CUDA results are checked against
independent CPU/dense references. Native NInfer attention covers BF16 and all
eight quantized K formats (worst absolute page-mass error about 1.3e-7 in the
tested geometries); llama covers f16/f32/q8_0/q5_0/q4_0 on the small model
(worst about 1.3e-7 on the CUDA route).

Small-model functional checks use the existing Qwen3.5-0.8B Q8_0 GGUF and the
existing four-layer NInfer slice (3 GDN +1 full attention +MTP). They cover Query,
warm reuse, rewritten requests and multiple decode-pressure events; NInfer
also covers cancellation and subsequent recovery. The slice validates native
state and control flow, not model quality. No new model download is required.
The complete IQ3 GGUF additionally passes MTP4 with B=768/R=128:512 output
tokens cross three DecodePressure events, followed by warm reuse and rewrite
recovery. Both the normal Qwen prompt and one containing literal role markers
produce only the five structured message spans, without extra trusted edges.

Initial, pre-audit measurements used Qwen3.8-27B GSQ-RCO IQ3_S, one warmup plus three
measured repetitions, cold approximately59K-token input and short-input512-token
output. The acceptance threshold is at most5% median throughput loss. The
NInfer 0.18 resource recipe is used unchanged; only ranking differs.

| NInfer workload | Fixed master median (range), tokens/s | C median (range), tokens/s | C change |
|---|---:|---:|---:|
| Long-input prefill | 570.03 (567.79–570.78) | 547.00 (545.23–547.53) | -4.04% |
| 512-token decode | 55.81 (49.40–57.34) | 55.42 (49.29–57.83) | -0.71% |
| Short-input prefill | 112.54 (84.24–128.63) | 107.07 (96.46–111.39) | -4.87%, noisy |

The short-input prefill result has substantial variation and does not establish
a precise regression size. The long-input prefill and decode medians satisfy
the threshold in this measured workload. A separately measured attempt to
group native sampler CTAs by page increased Op latency from about47.53ms to
49.27ms at the tested53K-token geometry, so it was discarded. This isolated
measurement is not an end-to-end baseline comparison.

llama.cpp uses the same context/B/R/H/prefill/sink/recent values, q8_0 KV,
CPU vision (1024-token cap) and fixed MTP4. Its server does not expose the same
adaptive MTP/ngram options as NInfer. These are comparisons within each backend,
not a comparison of the backends against each other. Requests use greedy
sampling, seed42 and thinking disabled in both baseline and candidate.

| llama.cpp workload | Fixed master median (range), tokens/s | C median (range), tokens/s | C change |
|---|---:|---:|---:|
| Long-input prefill | 490.06 (489.90–490.61) | 484.65 (482.25–485.38) | -1.10% |
| 512-token decode | 37.75 (37.20–39.29) | 37.91 (37.06–39.28) | +0.42% |
| Short-input prefill | 113.89 (113.84–114.48) | 113.62 (112.22–114.13) | -0.24% |

All three measured llama.cpp medians meet the threshold. NInfer's noisiest
short-prefill paired repetition changed from128.63 to107.07 tokens/s (-16.76%);
the three-run median is -4.87%. This is retained as uncertainty, not erased by
the stable long-input result. Neither set of three measurements establishes
performance for every request length or model.

The focused C launcher UI check passes old-draft ranking omission, preset
resource values and output-preview budgets above R. The common PowerShell
launcher and worker configuration-conflict tests pass. The broader pre-existing
UI harness stops at a stale `[data-default-worker]` assertion; no full UI-suite
pass is claimed. Backend patch hashes and patched trees are verified both in
place and by applying each patch to a fresh copy of its pinned base.

Qualification found and fixed a shared-memory maximum-to-sum synchronization
race in the new CUDA softmax reductions. The initially intermittent native
attention oracle failure passed ten repetitions after adding the barriers;
the final all-format native oracle and shared Query oracle also pass.

A subsequent boundary audit found two C integration defects: NInfer temporary
reconstruction rollback omitted graph/feedback/scope, and llama.cpp full
sequence removal retained them. It also found an inherited NInfer ordering
defect: temporary checkpoint Mean-K was committed before the current candidate
K was read. These paths now restore/reset the complete statistical state and
read current candidate K before any commit. The native NInfer regression uses
the existing four-layer fixture with ngram acceptance and a diagnostic-only
legal interior split; it checks normal and injected-exception rollback and
saved partial-page sums against current GPU candidate K. It does not establish
natural thinking-boundary behavior or model quality for the untrained fixture.
The extended `kvmem-llama-commit-order-test` checks accepted sampling, full
sequence removal and shorter-history checkpointing with synchronous and queued
GDN Fold on the existing 27B IQ3 model (RTX5060Ti). Its GDN replay prerequisite
excludes the 0.8B model; the separate small-model reset/marker check and both
backends' HTTP reuse/rewrite/pressure checks run on RTX5050.
The throughput tables above describe the earlier candidate. The final acceptance
section below includes these corrections and the legacy removal.

Other architectures, GPU vendors, multi-GPU pipelines, all media combinations
and broad model-quality benchmarks are outside this focused qualification.
The generic llama GGML fallback is not numerically qualified to the same CUDA
tolerance. The supported default speculative widths fit the native ubatch;
custom speculation split across several tiny ubatches is not newly qualified.

## Query policy consolidation (2026-10-21)

The llama.cpp server uses the `user` request/checkpoint flow for both text and media.
The old `legacy` query-policy executor and its private GDN snapshots/session fields
have been removed. `--kvmem-query-policy user` remains a compatibility option;
`--kvmem-query-policy legacy` fails at argument parsing with a migration message.
This resolves audit A4 by removing the path that rewound native history without
restoring graph/attention state. User-query suffix fallback and the independent
`--kvmem-query-replay legacy|auto` setting remain supported. Historical milestone
reports describe their original versions and do not define current CLI support.

## Historical post-audit performance acceptance before request unification (2026-10-21)

This earlier candidate includes the accepted-state/rollback corrections and the old
Query-policy executor removal. It predates the unified request/replay policy;
these measurements do not qualify the current unification build. It is compared with the same fixed master and
0.18 resource recipes on the same RTX 5060 Ti UUID recorded above. Product sources
and binaries are fixed during measurement. Initial runs use one warmup and three
measured repetitions per workload/version. Initial losses of at least 4% receive
one further warmup plus two measured repetitions per version; all five measured
samples are retained. Warmups are excluded throughout.

NInfer:

| Workload | Samples per version | Fixed master median (range), tokens/s | Final C median (range), tokens/s | Change |
|---|---:|---:|---:|---:|
| Long-input prefill | 5 | 569.32 (566.80–572.10) | 545.54 (542.41–546.95) | -4.18% |
| 512-token decode | 3 | 55.66 (51.74–58.07) | 55.38 (49.96–57.75) | -0.50% |
| Short-input prefill | 3 | 133.67 (133.06–133.81) | 131.25 (131.19–132.53) | -1.81% |

llama.cpp:

| Workload | Samples per version | Fixed master median (range), tokens/s | Final C median (range), tokens/s | Change |
|---|---:|---:|---:|---:|
| Long-input prefill | 3 | 486.95 (486.27–487.90) | 482.41 (480.30–482.50) | -0.93% |
| 512-token decode | 3 | 37.53 (36.65–38.90) | 37.42 (36.65–38.59) | -0.31% |
| Short-input prefill | 3 | 111.53 (109.82–111.62) | 110.83 (110.76–110.88) | -0.63% |

All final medians satisfy the maximum 5% loss gate for these workloads. Prefill
uses the actual processed prompt-token count; the approximately 59K-token history
may reuse up to six opening template tokens. Every long-input request returns
GRAPH-OK and every output workload completes 512 tokens. Decode uses the complete
speculative generation-loop timing, rather than a native single-step sub-timer.
The backends retain their own existing generation timing scopes and are compared
only against their respective baselines.

The 512-token output remains below R=16384, so it measures ordinary decode.
DecodePressure correctness was separately exercised at smaller B/R; its speed,
other request lengths, model quality and broad media/multi-GPU combinations are
not established by this focused performance run.


## Request/replay unification qualification (2026-10-21)

The current source passes the focused two-backend Query/Pressure qualification.
The existing 0.8B llama model and four-layer native NInfer fixture exercise real
prefix reuse, warm/cold tool continuation, session restore, rewriting, stream
cancellation, independent Query/replay caps and all-resident B<length<B+R.
Cancellation is also injected after replay has entered a nonzero prefix; a
subsequent same-session rewritten request succeeds. The NInfer fixture checks
native control/state, not semantic quality. Full IQ3 smoke checks separately
retain an actual document fact, then use a new tool correction in both backends.
Native accepted-prefix checks cover zero/partial/full MTP acceptance and native
state/Mean-K/graph/feedback checkpoint consistency.

An additional NInfer regression sets B768/R128 and MTP2/ngram31. Draft workspace
already causes Pressure while logical length is below896. A subsequent true
Query at884 tokens must score/replay that compressed prefix. It then completes
512 output tokens and crosses multiple DecodePressure events, proving that fixed
Query replay ends before ordinary generation pressure resumes. The pooled-buffer
test separately checks aliases, snapshot independence, bounded idle storage and
outstanding-lease lifetime after the pool owner is released.

Performance uses the unchanged master61569ff4bddcad60e8fad838e02f77a6190126d1,
the existing IQ3/0.18 resource recipes and RTX5060Ti UUID
GPU-5847813c-9e6e-bb43-cc5e-621aac091b6c. Each backend is compared only to its own
baseline. One warmup plus three measured repetitions cover approximately59K
cold input and512-token output. NInfer long input received one further warmup
plus two measurements under the predefined near-threshold rule; its fixed master
already has five long-input samples. Actual processed token counts and complete
speculative generation-loop timing are used, as in the historical baseline.

| Backend | Workload | Master median, tokens/s | Unified C median, tokens/s | Change | Samples master/C |
|---|---|---:|---:|---:|---:|
| ninfer | Long prefill | 569.32 | 543.81 | -4.48% | 5/5 |
| ninfer | 512-token decode | 55.66 | 55.47 | -0.34% | 3/3 |
| ninfer | Short prefill | 133.67 | 152.72 | +14.25% | 3/3 |
| llamacpp | Long prefill | 486.95 | 484.79 | -0.44% | 3/3 |
| llamacpp | 512-token decode | 37.53 | 37.66 | +0.34% | 3/3 |
| llamacpp | Short prefill | 111.53 | 115.43 | +3.49% | 3/3 |

All six medians satisfy the maximum5% loss gate in these workloads. All long-input
requests return GRAPH-OK and all output requests complete512 tokens. NInfer long
prefill is close to the limit; these samples do not establish a universal bound.

Earlier failures remain recorded: the initial candidate short-prefill median
lost10.05%; a matched short control lost about35.5%, also reproduced by an older
C execution path. Skipping unused Q capture alone still lost7.32%; caching one
released StateImage did not stabilize the result. A separate instrumented build
located large pinned allocation spikes and is excluded from acceptance. The
current pair cache covers the input/endpoint buffers released by anchor restore;
with conditional Q collection the final short-prefill median is152.72 tokens/s.
These build groups are kept separate. A fresh baseline supplement with mixed
69/63 processed-token cache states is excluded from the historical baseline pool.
No slow measured sample is removed from its recorded group.

Local evidence is under graph-design-20261020/unify-20261021: results.json,
comparison-final.json, adverse-evidence.json, validation-final.json, finish-run.json
and supplement-final-checks.json, plus full request/response/native logs. Backend
patches are independently applied to their pinned bases and checked against the
manifest. Fixed baseline executable hashes and expected backend patch trees are
unchanged. No commit or PR was created.

The512-token default output workload remains belowR16384. DecodePressure
correctness is qualified separately at smallR; its throughput is not measured
here. This work does not restart RULER or add broad quality, media, vendor or
multi-GPU qualification. The optional idle pair holds at most293.6MiB on this
IQ3 Program, appears in checkpoint_bytes and is released with the Program.
