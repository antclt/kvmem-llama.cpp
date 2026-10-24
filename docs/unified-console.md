# Unified local console

The one-click console serves launch configuration, chat and monitoring from the same
local origin. Its default addresses are `/monitor/` and `/chat/` on port 18199;
the inference API still runs separately, normally on port 18200. Use the console's
Chat tab after starting a model. Switching tabs preserves an active response.

The chat reads settings from the running service, displays reported values, and
stores explicit overrides per conversation and model. Thinking, its token budget,
reasoning effort and history preservation follow each backend's supported controls.
Unreported defaults are labeled as unreported. While the service is starting or
running, launch settings show its `active_config` and are read-only, including
model/GPU selection, native arguments and template actions. Tabs and help remain
available. A successful stop or process exit unlocks the last run's parameters
for editing and restarting; a failed stop keeps them locked. A pending start or
unconfirmed service state also keeps editing locked. Browser drafts cannot replace
the running configuration, and API keys are never returned by the status endpoint.

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

The monitor keeps readings and exceptional states visible. Metric definitions are
available from the adjacent information buttons in readable popovers; runtime
settings and cache details expand on demand. Device-assignment warnings remain
visible when assignments are unknown or no GPU is used.

Context and default output limits share the resource row. Recall policy has a
dedicated tab for scoring, graph propagation, LRFU and retention budgets; scoring,
graph propagation and LRFU remain independently selectable. PolyStrata alone has
an **Elastic experts** tab with off, workspace reuse and VRAM borrowing modes.
These controls round-trip through the existing native JSON arguments without
discarding unrelated flags. Switching engines preserves their individual settings.
The monitor displays configured MTP draft limits and policy, with request-level
draft counts only when the backend reports them.

## Backend logs

The **Backend logs** tab (`/monitor/#logs`) reads the controller's shared stdout /
stderr capture for llama.cpp, NInfer and PolyStrata. It supports text search,
in-place highlighting / plain text, line wrapping, pause, automatic following and export.
Scrolling up disables following; **Jump to latest** restores it. The view shares
the monitor language, theme and density. Log contents are always text, never HTML.
Original prefixes, indentation and blank lines stay in one text stream. Explicit level
labels, timestamps, module tags and performance metrics receive subtle highlighting;
no timestamp or severity is invented. ANSI control sequences and carriage-return
progress are normalized for display.

`GET /api/launcher/logs` returns at most 256 KiB of complete recent output, plus
process/file identity and a revision. A matching `?revision=` returns `text: null`.
The page displays at most 2,000 lines; export saves the current cached
original snapshot (including known-key redactions), not the entire file. It polls
once per second only while visible and unpaused, with no overlapping requests and
bounded retry backoff. It does not query the inference worker. A stopped process's
last unterminated line is retained; a live incomplete line waits for a delimiter.
Stopping retains the captured output and its backend identity. Starting a new
worker resets the viewer to the new file, matching the existing file lifecycle.
After restarting the controller, an old file is labeled as a previous run if its
engine identity is unavailable.

For an already-running older controller, the view falls back to the existing
`/api/launcher/log` endpoint and explicitly shows its **32 KiB** limit. This allows
static UI updates without stopping that controller's owned inference process.
The modern endpoint is used on the next console restart. A native worker serving
only the monitor page has no access to the controller's process output; the log
view explains that the local console is required.

Model-free validation: `python tests/test_console_logs.py`,
`node ui/test-log-parser.cjs`, and `node ui/test-logs.cjs` (Playwright; accepts
`PLAYWRIGHT_MODULE`, `PLAYWRIGHT_BROWSERS_PATH`, `CHROMIUM`, `SCREENSHOT_DIR`).

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
