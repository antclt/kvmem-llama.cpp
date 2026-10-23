# Unified local console

The one-click console serves launch configuration, chat and monitoring from the same
local origin. Its default addresses are `/monitor/` and `/chat/` on port 18199;
the inference API still runs separately, normally on port 18200. Use the console's
Chat tab after starting a model. Switching tabs preserves an active response.

The chat reads settings from the running service, displays reported values, and
stores explicit overrides per conversation and model. Thinking, its token budget,
reasoning effort and history preservation follow each backend's supported controls.
Unreported defaults are labeled as unreported. Editing launch configuration does
not change a running service's chat settings.

Completed messages show the native average decode rate, output count and stop
reason. A one-token reply or an absent/nonpositive native rate has no displayed
decode speed. The details show native prefill/reuse counts and client wait times.
Live message rates require an exact request match: PolyStrata supplies that match;
other workers can still show their native final means and service-wide monitoring.

Histories remain in the browser. Import and backup support KVMem, llama-ui and
PolyStrata exports. Imported IDs are stable, branches and reasoning are retained,
and unsupported tool/attachment data remains available in the backup. The chat
does not execute imported tool workflows. The old origin's export page reads its
storage without deleting it. In KVMem mode, PolyStrata's bundled chat and monitor
routes are disabled; its inference APIs, settings and history export remain available.

All views share the console theme, density and language. The mobile history and
settings drawers support Escape, keyboard focus containment and focus return.
Recent-model selectors show filenames with a distinguishing parent suffix when
necessary; the full path remains in the value and tooltip.

## Building and packaging

Build the lightweight UI with `python scripts/build-webui.py` before packaging.
The manifest must declare `console_chat_protocol: 1`. The full upstream UI is
still available through `--full-ui`, but is not the unified console chat bundle.
`package-console.ps1 -ChatUiDir <directory>` can supply the built lightweight UI;
worker packages can provide it in `workers/share/kvmem/ui`. The one-click check
rejects missing or incompatible chat assets before opening the console.

## Validation and limits

Core coverage includes cancellation during prefill, disconnect/error handling,
service identity, native settings and thinking translation, historical imports,
offline packaging, and built-browser checks for all three backend settings
contracts. CI runs these alongside the existing console and monitor tests.

Earlier runtime validation on an RTX 5060 Ti used Qwen3.5 0.8B for llama.cpp,
Qwen3.5 4B MTP Q4 for NInfer and Tiel Coder 35B-A3B Q4 for PolyStrata. Streaming,
thinking, follow-up requests and monitoring passed. A short warmed 64-token
native/console/console/native comparison observed decode changes of -1.2%, -0.02%
and +3.0%, respectively, with about 20–30 ms extra time to first output through
the console. This does not establish cold-start or sustained-load performance.

This UI change does not alter native C++ inference code. Expert-profile persistence
is a separate pending integration item; initial expert-cache adaptation and system
memory pressure can still cause slow starts. Batch MTP and multi-GPU PolyStrata
remain outside the current integration.
