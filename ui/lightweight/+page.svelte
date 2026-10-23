<script lang="ts">
  import { onMount, tick } from "svelte";
  import MarkdownContent from "$lib/components/app/content/MarkdownContent/MarkdownContent.svelte";
  import { DatabaseService as DB } from "$lib/services/database.service";
  import { settingsStore } from "$lib/stores/settings/index.svelte";
  import { context, streamChat, requestRecord, cancelRequest } from "./api";
  import { normalizeHistory } from "./history";
  import { english } from "./labels";
  import { setMode } from "mode-watcher";
  type Row = any;
  let ctx = $state<Row>(null),
    conversations = $state<Row[]>([]),
    active = $state<Row>(null),
    messages = $state<Row[]>([]);
  let input = $state(""),
    images = $state<Row[]>([]),
    busy = $state(false),
    loading = $state(false),
    error = $state(""),
    notice = $state("");
  let settingsOpen = $state(false),
    historyOpen = $state(false),
    migrationOpen = $state(false),
    mobile = $state(false);
  let historyButton: HTMLButtonElement | null = $state(null);
  let theme = $state("dark"),
    density = $state("comfortable"),
    language = $state("zh-CN");
  let overrides = $state<Row>({}),
    requestId = $state(""),
    live = $state<Row>(null),
    now = $state(Date.now());
  let controller: AbortController | null = null,
    bottom: HTMLDivElement,
    scroll: HTMLDivElement,
    reply: Row = null;
  let saveTimer: ReturnType<typeof setInterval>,
    drawTimer: ReturnType<typeof setTimeout> | null = null,
    writes = Promise.resolve(),
    contextBusy = false,
    historyInitialized = false;
  const t = (s: string) => (language === "en" ? english[s] || s : s);
  const scope = () => ctx?.backend + ":" + ctx?.model;
  const draftKey = () => "kvmem-console-draft:" + (active?.id || "new");
  function get(key: string, fallback: any) {
    try {
      return JSON.parse(localStorage.getItem(key) || "null") ?? fallback;
    } catch {
      return fallback;
    }
  }
  function set(key: string, value: any) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      error = t("浏览器存储已满，请导出历史备份");
    }
  }
  const plain = (v: any) => JSON.parse(JSON.stringify(v));
  const shown = (key: string) =>
    typeof value(key) === "number" ? Number(value(key).toFixed(6)) : value(key);
  function drawer(
    node: HTMLElement,
    options: {
      enabled: boolean;
      close: () => void;
      trigger?: HTMLElement | null;
    } = {
      enabled: true,
      close: () => {
        settingsOpen = false;
      },
    },
  ) {
    let before: HTMLElement | null = null,
      enabled = false;
    const restore = () => {
      const target = before;
      before = null;
      queueMicrotask(() => target?.isConnected && target.focus());
    };
    const controls = () =>
      Array.from(
        node.querySelectorAll<HTMLElement>(
          "button:not(:disabled),input:not(:disabled),select:not(:disabled),summary",
        ),
      ).filter((el) => el.getClientRects().length && !el.closest("[inert]"));
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        options.close();
        e.preventDefault();
        e.stopPropagation();
      }
      if (e.key === "Tab") {
        const a = controls();
        const first = a[0],
          last = a.at(-1);
        if (
          e.shiftKey &&
          (document.activeElement === first || document.activeElement === node)
        ) {
          last?.focus();
          e.preventDefault();
        } else if (!e.shiftKey && document.activeElement === last) {
          first?.focus();
          e.preventDefault();
        }
      }
    };
    const update = (next: typeof options) => {
      options = next;
      if (options.enabled === enabled) return;
      enabled = options.enabled;
      if (enabled) {
        before = options.trigger ?? (document.activeElement as HTMLElement);
        queueMicrotask(() => {
          if (enabled && node.isConnected) (controls()[0] ?? node).focus();
        });
        node.addEventListener("keydown", key);
      } else {
        node.removeEventListener("keydown", key);
        restore();
      }
    };
    update(options);
    return {
      update,
      destroy() {
        node.removeEventListener("keydown", key);
        if (enabled) restore();
      },
    };
  }
  const source = (key: string) =>
    t(Object.hasOwn(overrides, key) ? "本对话已修改" : "服务设置");
  function value(key: string): any {
    if (Object.hasOwn(overrides, key)) return overrides[key];
    if (["temperature", "top_p", "top_k", "min_p"].includes(key)) {
      const effort =
          overrides.reasoning_effort ?? ctx?.effective?.reasoning_effort,
        on = overrides.enable_thinking ?? ctx?.effective?.enable_thinking;
      const preset =
        ctx?.sampling_by_effort?.[on === false ? "none" : effort] ??
        ctx?.sampling_by_mode?.[on ? "thinking" : "non_thinking"];
      if (preset && key in preset) return preset[key];
    }
    return ctx?.effective?.[key];
  }
  function appearance(data: Row) {
    if (data.theme) theme = data.theme;
    if (data.density) density = data.density;
    if (data.language) language = data.language;
    const dark =
      theme === "dark" ||
      (theme === "system" &&
        matchMedia("(prefers-color-scheme: dark)").matches);
    setMode(dark ? "dark" : "light");
    document.documentElement.classList.toggle("dark", dark);
    document.documentElement.style.colorScheme = dark ? "dark" : "light";
    document.documentElement.lang = language;
  }
  async function refreshContext() {
    if (contextBusy) return;
    contextBusy = true;
    try {
      const old = scope();
      ctx = await context();
      if (ctx.state === "ready" && scope() !== old) {
        overrides = active?.consoleSettings?.[scope()] ?? {};
        if (old !== "undefined:undefined")
          notice = t("模型已切换，已读取该模型的对话设置");
      }
      if (ctx.state === "ready") validateOverrides();
    } catch {
      ctx = { ...ctx, state: "unavailable" };
      if (!ctx?.model) notice = t("请从 KVMem 控制台打开聊天");
    } finally {
      ((contextBusy = false), (historyInitialized = false));
    }
  }
  async function saveSettings() {
    if (active) {
      active = {
        ...active,
        consoleSettings: {
          ...active.consoleSettings,
          [scope()]: { ...overrides },
        },
      };
      await DB.updateConversation(active.id, {
        consoleSettings: plain(active.consoleSettings),
      } as any);
    }
  }
  function validateOverrides() {
    const invalid = Object.keys(overrides).filter(
      (k) =>
        (k === "preserve_thinking" && !ctx.capabilities?.preserve) ||
        (k === "reasoning_effort" &&
          overrides[k] !== "none" &&
          !ctx.capabilities?.efforts?.includes(overrides[k])) ||
        (k === "max_tokens" && overrides[k] > ctx.limit),
    );
    if (invalid.length) {
      const n = { ...overrides };
      invalid.forEach((k) => delete n[k]);
      overrides = n;
      notice =
        t("部分覆盖参数不再适用，已恢复服务设置") + ": " + invalid.join(", ");
      void saveSettings();
    }
  }
  function edit(key: string, raw: any) {
    error = "";
    let v = raw;
    if (
      !["enable_thinking", "preserve_thinking", "reasoning_effort"].includes(
        key,
      )
    ) {
      if (
        raw === "" ||
        (key === "reasoning_budget_tokens" &&
          Number(raw) === -1 &&
          ctx.backend === "llamacpp")
      ) {
        const n = { ...overrides };
        delete n[key];
        overrides = n;
        void saveSettings();
        return;
      }
      v = Number(raw);
      if (
        !Number.isFinite(v) ||
        (key !== "seed" && v < 0) ||
        (["max_tokens", "reasoning_budget_tokens", "top_k", "seed"].includes(
          key,
        ) &&
          !Number.isInteger(v)) ||
        (key === "max_tokens" && (v < 1 || v > ctx.limit)) ||
        (key === "reasoning_budget_tokens" && v < (ctx.budget_min ?? 0)) ||
        (["top_p", "min_p"].includes(key) && v > 1) ||
        (key === "temperature" && v > 10)
      ) {
        error = t("参数超出有效范围，已保留原值") + ": " + key;
        return;
      }
    }
    overrides = { ...overrides, [key]: v };
    if (key === "enable_thinking") {
      const n = { ...overrides };
      if (ctx.backend === "polystrata")
        n.reasoning_effort = v
          ? ctx.effective.reasoning_effort === "none"
            ? "high"
            : ctx.effective.reasoning_effort || "high"
          : "none";
      else if (!v) delete n.reasoning_effort;
      overrides = n;
    }
    void saveSettings();
  }
  function editNumber(key: string, field: HTMLInputElement) {
    edit(key, field.value);
    field.value = String(shown(key) ?? "");
  }
  function reset() {
    overrides = {};
    void saveSettings();
    notice = t("已恢复当前服务设置");
  }
  async function refreshHistory() {
    try {
      conversations = await DB.getAllConversations();
    } catch (e) {
      error = t("读取历史失败") + ": " + e;
    }
  }
  async function selectConversation(conv: Row) {
    if (busy || loading) return;
    loading = true;
    try {
      if (historyInitialized) set(draftKey(), input);
      historyInitialized = true;
      messages = conv
        ? (await DB.getConversationMessages(conv.id)).map((m: Row) => ({
            ...m,
            uiStatus: m.uiStatus === "pending" ? "interrupted" : m.uiStatus,
          }))
        : [];
      active = conv;
      overrides = conv?.consoleSettings?.[scope()] ?? {};
      input = get(draftKey(), "");
      images = [];
      error = "";
      historyOpen = false;
      set("kvmem-console-active", conv?.id ?? null);
      await tick();
      bottom?.scrollIntoView();
    } catch (e) {
      error = t("读取历史失败") + ": " + e;
    } finally {
      loading = false;
    }
  }
  async function remove() {
    if (!active || busy || !confirm(t("删除此对话？请先导出需要保留的记录。")))
      return;
    await DB.deleteConversation(active.id);
    await selectConversation(null);
    await refreshHistory();
  }
  function navigate(page: string) {
    parent.postMessage({ type: "kvmem-navigate", page }, location.origin);
  }
  async function addImages(files: File[]) {
    if (!ctx?.capabilities?.vision) return;
    try {
      for (const f of files) {
        if (
          !["image/png", "image/jpeg", "image/webp", "image/gif"].includes(
            f.type,
          ) ||
          f.size > 10 * 1024 * 1024 ||
          images.length >= 4
        )
          throw Error(t("最多 4 张图片，每张不超过 10 MiB"));
        const url = await new Promise<string>((resolve, reject) => {
          const r = new FileReader();
          r.onload = () => resolve(String(r.result));
          r.onerror = reject;
          r.readAsDataURL(f);
        });
        images = [...images, { name: f.name, base64Url: url, type: "image" }];
      }
    } catch (e) {
      error = String(e);
    }
  }
  function wire(m: Row) {
    if (m.tool_calls || m.toolCalls || m.tools?.length || m.role === "tool")
      throw Error(t("此历史包含工具记录，请使用支持工具的客户端继续"));
    const extras = m.extra || [];
    if (
      m.legacyContent ||
      m.legacySource?.images?.some((x: Row) => !x.url) ||
      extras.some((x: Row) => x.type !== "image" || !x.base64Url)
    )
      throw Error(t("此历史包含当前聊天页尚不支持的附件，原记录已保留"));
    return {
      role: m.role,
      content: extras.length
        ? [
            { type: "text", text: m.content },
            ...extras.map((x: Row) => ({
              type: "image_url",
              image_url: { url: x.base64Url },
            })),
          ]
        : m.content,
      ...(m.reasoningContent ? { reasoning_content: m.reasoningContent } : {}),
    };
  }
  function options() {
    const body: Row = { model: ctx.model },
      template: Row = {};
    for (const [k, v] of Object.entries(overrides)) {
      if (["enable_thinking", "preserve_thinking"].includes(k)) template[k] = v;
      else body[k] = v;
    }
    if (Object.keys(template).length) body.chat_template_kwargs = template;
    return body;
  }
  function draw() {
    if (drawTimer) return;
    drawTimer = setTimeout(() => {
      drawTimer = null;
      const follow =
        scroll &&
        scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 140;
      messages = messages.map((m) => (m.id === reply?.id ? { ...reply } : m));
      if (follow) void tick().then(() => bottom?.scrollIntoView());
    }, 60);
  }
  function persistReply() {
    if (!reply) return;
    const { id, ...saved } = JSON.parse(JSON.stringify(reply));
    writes = writes
      .then(() => DB.updateMessage(id, saved))
      .catch((e) => {
        error = t("保存历史失败") + ": " + e;
      });
  }
  async function send(continuation = false) {
    if (busy || loading) return;
    await refreshContext();
    if (ctx?.state !== "ready") {
      error = t("请先启动模型");
      return;
    }
    if (!continuation && !input.trim() && !images.length) return;
    error = "";
    notice = "";
    try {
      messages.forEach(wire);
    } catch (e) {
      error = String(e);
      return;
    }
    if (
      (images.length ||
        messages.some((m) => m.extra?.some((x: Row) => x.type === "image"))) &&
      !ctx.capabilities?.vision
    ) {
      error = t("当前模型不支持图片");
      return;
    }
    busy = true;
    reply = null;
    controller = new AbortController();
    requestId = crypto.randomUUID();
    live = { state: "waiting", started_at_ms: Date.now() };
    try {
      if (!active) {
        active = await DB.createConversation(
          input.trim().slice(0, 40) || t("新对话"),
        );
        set("kvmem-console-active", active.id);
        await saveSettings();
      }
      const user = await DB.createMessageBranch(
        {
          convId: active.id,
          type: "text",
          role: "user",
          content: continuation ? t("请从刚才停止的位置继续。") : input,
          timestamp: Date.now(),
          children: [],
          parent: null,
          extra: plain(images),
        } as any,
        messages.at(-1)?.id ?? null,
      );
      messages = [...messages, user];
      input = "";
      images = [];
      set(draftKey(), "");
      set("kvmem-console-draft:new", "");
      const history = messages
          .filter((m) => m.content || m.reasoningContent || m.extra?.length)
          .map(wire),
        opts = options();
      reply = await DB.createMessageBranch(
        {
          convId: active.id,
          type: "text",
          role: "assistant",
          content: "",
          reasoningContent: "",
          timestamp: Date.now() + 1,
          children: [],
          parent: user.id,
          uiStatus: "pending",
          requestId,
          execution: {
            backend: ctx.backend,
            model: ctx.model,
            effective: Object.fromEntries(
              [
                ...new Set([
                  ...Object.keys(ctx.effective),
                  ...Object.keys(overrides),
                ]),
              ].map((k) => [k, value(k)]),
            ),
          },
        } as any,
        user.id,
      );
      messages = [...messages, reply];
      await tick();
      bottom?.scrollIntoView();
      saveTimer = setInterval(persistReply, 750);
      await streamChat(
        { ...opts, messages: history },
        controller.signal,
        (data) => {
          const choice = data.choices?.[0],
            delta = choice?.delta;
          if (delta?.tool_calls)
            throw Error(t("此聊天页不执行工具，请使用支持工具的客户端"));
          reply.content += delta?.content ?? "";
          reply.reasoningContent += delta?.reasoning_content ?? "";
          if (delta?.reasoning_content || delta?.content) {
            live = {
              ...live,
              state: delta.reasoning_content ? "thinking" : "answering",
              first_token_at_ms: live.first_token_at_ms ?? Date.now(),
            };
            if (reply.thinkOpen === undefined)
              reply.autoOpen = !!delta.reasoning_content;
          }
          if (choice?.finish_reason) reply.finishReason = choice.finish_reason;
          if (data.timings) reply.timings = data.timings;
          if (data.usage) reply.usage = data.usage;
          draw();
        },
        requestId,
        ctx.instance,
      );
      reply.uiStatus = "complete";
    } catch (e) {
      if (reply)
        reply.uiStatus = controller?.signal.aborted ? "interrupted" : "error";
      if (!controller?.signal.aborted) error = String(e);
    } finally {
      clearInterval(saveTimer);
      if (drawTimer) {
        clearTimeout(drawTimer);
        drawTimer = null;
      }
      if (reply) {
        reply.autoOpen = false;
        reply.elapsedMs = Date.now() - live.started_at_ms;
        reply.firstTokenMs = live.first_token_at_ms
          ? live.first_token_at_ms - live.started_at_ms
          : null;
        persistReply();
        await writes;
        messages = messages.map((m) => (m.id === reply.id ? { ...reply } : m));
      }
      try {
        await cancelRequest(requestId);
        if (active)
          await DB.updateConversation(active.id, { lastModified: Date.now() });
        await refreshHistory();
      } catch (e) {
        error = t("保存历史失败") + ": " + e;
      } finally {
        busy = false;
        controller = null;
        reply = null;
      }
    }
  }
  function stop() {
    controller?.abort();
    void cancelRequest(requestId);
  }
  function finish(m: Row) {
    if (m.uiStatus === "pending") return t("生成中");
    if (m.uiStatus === "interrupted") return t("已停止或中断");
    if (m.uiStatus === "error") return t("生成失败");
    if (m.finishReason === "length")
      return t(
        m.reasoningContent && !m.content
          ? "思考阶段达到输出上限"
          : "达到输出上限",
      );
    return t("正常结束");
  }
  function speed(m: Row) {
    const n = m.timings?.predicted_n ?? m.usage?.completion_tokens,
      r = m.timings?.predicted_per_second;
    return n > 1 && Number.isFinite(r) && r > 0 ? r.toFixed(1) : null;
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      error = String(e);
    }
  }
  function download(v: any, name: string, type = "application/json") {
    const url = URL.createObjectURL(
      new Blob([typeof v === "string" ? v : JSON.stringify(v, null, 2)], {
        type,
      }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function exportAll() {
    const all = await DB.getAllConversations(),
      data = await DB.getConversationsWithMessages(all.map((c) => c.id));
    download(
      {
        schema_version: 1,
        source: location.origin,
        conversations: Array.from(data.values()),
      },
      "kvmem-chats.json",
    );
  }
  async function importFile(f: File | undefined) {
    if (!f) return;
    loading = true;
    error = "";
    try {
      if (f.size > 128 * 1024 * 1024) throw Error(t("历史文件过大"));
      const c = await normalizeHistory(JSON.parse(await f.text()));
      const existing = await DB.getConversationsWithMessages(
          (await DB.getAllConversations()).map((x) => x.id),
        ),
        ids = new Set(
          Array.from(existing.values()).flatMap((x) =>
            x.messages.map((m) => m.id),
          ),
        );
      if (
        c.items.some(
          (x: Row) =>
            !existing.has(x.conv.id) &&
            x.messages.some((m: Row) => ids.has(m.id)),
        )
      )
        throw Error("Message identity collision");
      const result = await DB.importConversations(c.items);
      notice = `${t("导入完成")} · ${result.imported.length} ${t("对话")} · ${result.skipped.length} ${t("重复已跳过")} · ${c.items.reduce((n: number, x: Row) => n + x.messages.length, 0)} ${t("消息")}`;
      if (c.warnings || c.legacy)
        notice +=
          " · " + t("工具或特殊附件原始数据已保留，当前页面不能继续此类对话");
      await refreshHistory();
    } catch (e) {
      error = t("导入失败，原记录未更改") + ": " + e;
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    settingsStore.initialize();
    const viewport = matchMedia("(max-width: 760px)");
    const resize = () => {
      mobile = viewport.matches;
      if (!mobile) historyOpen = false;
    };
    resize();
    viewport.addEventListener("change", resize);
    const pref = localStorage.getItem("kvmem-monitor-theme") || "dark";
    appearance({
      theme: pref,
      density: localStorage.getItem("kvmem-monitor-density") || "comfortable",
      language: localStorage.getItem("kvmem-monitor-language") || "zh-CN",
    });
    const message = (e: MessageEvent) => {
      if (
        e.origin === location.origin &&
        e.source === parent &&
        e.data?.type === "kvmem-appearance"
      )
        appearance(e.data);
    };
    window.addEventListener("message", message);
    const systemTheme = matchMedia("(prefers-color-scheme: dark)");
    const syncSystemTheme = () => {
      if (theme === "system") appearance({});
    };
    systemTheme.addEventListener("change", syncSystemTheme);
    parent.postMessage({ type: "kvmem-chat-ready" }, location.origin);
    void refreshContext();
    void refreshHistory().then(() =>
      selectConversation(
        conversations.find((c) => c.id === get("kvmem-console-active", null)) ??
          null,
      ),
    );
    const interval = setInterval(() => {
      now = Date.now();
      if (!busy) void refreshContext();
      else if (requestId)
        void requestRecord(requestId).then((r) => {
          if (r) live = { ...live, ...r };
        });
    }, 1000);
    const leave = () => {
      set(draftKey(), input);
      if (busy) {
        persistReply();
        stop();
      }
    };
    window.addEventListener("pagehide", leave);
    return () => {
      leave();
      clearInterval(interval);
      clearInterval(saveTimer);
      if (drawTimer) clearTimeout(drawTimer);
      window.removeEventListener("message", message);
      viewport.removeEventListener("change", resize);
      systemTheme.removeEventListener("change", syncSystemTheme);
      window.removeEventListener("pagehide", leave);
    };
  });
</script>

<svelte:head
  ><title>KVMem · Chat</title><link
    rel="stylesheet"
    href="/monitor/monitor.css"
  /></svelte:head
>
<div id="app" data-theme={theme} data-density={density}>
  {#if historyOpen && mobile}<button
      class="history-backdrop"
      tabindex="-1"
      aria-label={t("关闭面板")}
      onclick={() => (historyOpen = false)}
    ></button>{/if}
  <div class="chat-shell">
    <aside
      class:mobile-open={historyOpen}
      aria-label={t("聊天历史")}
      tabindex="-1"
      role={historyOpen && mobile ? "dialog" : undefined}
      aria-modal={historyOpen && mobile ? true : undefined}
      use:drawer={{
        enabled: historyOpen && mobile,
        trigger: historyButton,
        close: () => {
          historyOpen = false;
        },
      }}
    >
      <div class="side-head">
        <strong>{t("聊天历史")}</strong><button
          class="mobile-only"
          onclick={() => (historyOpen = false)}
          aria-label={t("关闭面板")}>×</button
        >
      </div>
      <button
        class="s-primary new-chat"
        disabled={busy || loading}
        onclick={() => selectConversation(null)}>＋ {t("新建对话")}</button
      >
      <nav>
        {#each conversations as c (c.id)}<button
            class:selected={active?.id === c.id}
            disabled={busy || loading}
            onclick={() => selectConversation(c)}
            title={c.name}>{c.name}</button
          >{/each}
      </nav>
      <div class="history-actions">
        <button
          disabled={busy || loading}
          onclick={() => {
            migrationOpen = !migrationOpen;
            historyOpen = false;
          }}>{t("导入与备份")}</button
        ><small>{t("历史仅保存在此浏览器")}</small>
      </div>
    </aside>
    <section
      class="chat-main"
      aria-label={t("聊天")}
      inert={historyOpen && mobile}
    >
      <header class="chat-header">
        <button
          bind:this={historyButton}
          class="mobile-only"
          onclick={() => (historyOpen = true)}
          aria-label={t("聊天历史")}>☰</button
        >
        <div>
          <strong>{ctx?.model || t("尚未启动模型")}</strong><small
            >{ctx?.backend || "KVMem"}{#if ctx?.context}
              · {Number(ctx.context).toLocaleString()} {t("上下文")}{/if}</small
          >
        </div>
        <span class="s-badge"
          >{t(
            busy ? "生成中" : ctx?.state === "ready" ? "就绪" : "未就绪",
          )}</span
        ><button
          onclick={() => (settingsOpen = true)}
          disabled={busy || ctx?.state !== "ready"}>{t("参数")}</button
        >
      </header>
      <div class="chat-scroll" bind:this={scroll}>
        {#if migrationOpen}<section class="migration s-card">
            <strong>{t("导入与备份")}</strong>
            <p>
              {t("迁移不会删除旧记录。先在旧地址导出 JSON，再在这里导入。")}
            </p>
            <div class="row">
              <button onclick={exportAll}>{t("导出全部历史")}</button><label
                class="button"
                >{t("导入历史")}<input
                  type="file"
                  accept=".json,application/json"
                  disabled={busy || loading}
                  onchange={(e) => {
                    void importFile(e.currentTarget.files?.[0]);
                    e.currentTarget.value = "";
                  }}
                /></label
              >{#if ctx?.history_export_url}<a
                  target="_blank"
                  rel="noopener"
                  href={ctx.history_export_url}>{t("打开旧地址导出页")} ↗</a
                >{/if}
            </div>
            <small
              >{t(
                "支持 KVMem、llama-ui 和 PolyStrata 导出；工具与特殊附件原始数据会保留。",
              )}</small
            >
          </section>{/if}
        {#if !messages.length}<div class="welcome">
            <h1>{t("开始一次对话")}</h1>
            <p>{t("本地模型，统一的聊天与监控体验")}</p>
            {#if ctx?.state !== "ready"}<button
                onclick={() => navigate("config")}>{t("前往启动配置")}</button
              >{/if}
          </div>{/if}
        <div class="messages">
          {#each messages as m (m.id)}<article class:user={m.role === "user"}>
              <div class="role">
                {m.role === "user" ? t("你") : m.execution?.model || "KVMem"}
              </div>
              {#if m.reasoningContent}<details
                  class="thinking"
                  open={m.thinkOpen ?? m.autoOpen ?? false}
                >
                  <summary
                    onclick={(e) => {
                      e.preventDefault();
                      m.thinkOpen = !(m.thinkOpen ?? m.autoOpen ?? false);
                      messages = [...messages];
                    }}
                    >{t(
                      "思考",
                    )}{#if m.uiStatus === "pending" && live?.state === "thinking"}
                      · {t("进行中")}{/if}</summary
                  >{#if m.thinkOpen ?? m.autoOpen ?? false}<div
                      class="reasoning"
                    >
                      {m.reasoningContent}
                    </div>{/if}
                </details>{/if}
              {#if m.role === "assistant"}<MarkdownContent
                  content={m.content}
                />{:else}<div class="user-text">
                  {m.content}
                </div>{/if}
              {#each m.extra ?? [] as item}{#if item.type === "image" && item.base64Url}<img
                    class="attachment"
                    src={item.base64Url}
                    alt={item.name || t("图片")}
                  />{/if}{/each}
              {#if m.role === "assistant" && m.uiStatus !== "pending"}<footer
                  class="message-footer"
                >
                  <span>{finish(m)}</span>{#if speed(m)}<span
                      >{t("平均")} {speed(m)} tok/s</span
                    >{/if}{#if m.timings?.predicted_n ?? m.usage?.completion_tokens}<span
                      >{m.timings?.predicted_n ?? m.usage?.completion_tokens} token</span
                    >{/if}<button onclick={() => copy(m.content)}
                    >{t("复制")}</button
                  ><button
                    onclick={() =>
                      download(m.content, "answer.md", "text/markdown")}
                    >{t("下载")}</button
                  >
                  <details>
                    <summary>{t("详情")}</summary>
                    <div class="message-details">
                      <div>{m.execution?.backend} · {m.execution?.model}</div>
                      {#if m.timings}<div>
                          {t("新算 / 复用 token")}：{m.timings.prompt_n ?? "—"} /
                          {m.timings.cache_n ?? "—"}
                        </div>
                        <div>
                          {t("预填充")}：{m.timings.prompt_ms != null
                            ? (m.timings.prompt_ms / 1000).toFixed(2) + "s"
                            : "—"}
                        </div>{/if}
                      <div>
                        {t("首个输出等待（含排队）")}：{m.firstTokenMs != null
                          ? (m.firstTokenMs / 1000).toFixed(2) + "s"
                          : "—"}
                      </div>
                      <div>
                        {t("总耗时")}：{m.elapsedMs != null
                          ? (m.elapsedMs / 1000).toFixed(2) + "s"
                          : "—"}
                      </div>
                      {#if m.execution?.effective}<div>
                          {t("本轮参数")}：{JSON.stringify(
                            m.execution.effective,
                          )}
                        </div>{/if}
                    </div>
                  </details>
                </footer>{/if}
            </article>{/each}
          <div bind:this={bottom}></div>
        </div>
      </div>
      <div class="composer-wrap">
        {#if error && !settingsOpen}<div class="chat-error" role="alert">
            {error}
          </div>{/if}{#if notice}<div class="chat-notice" role="status">
            {notice}<button
              onclick={() => (notice = "")}
              aria-label={t("关闭面板")}>×</button
            >
          </div>{/if}
        {#if busy}<div class="live-line" role="status">
            <span class="pulse"></span>{t(
              live?.state === "thinking"
                ? "思考中"
                : live?.state === "answering"
                  ? "输出正文"
                  : "等待首个输出",
            )} · {((now - (live?.started_at_ms || now)) / 1000).toFixed(
              0,
            )}s{#if live?.metrics?.decode_tps}<span
                >{t("实时")} {live.metrics.decode_tps.toFixed(1)} tok/s</span
              >{/if}{#if live?.metrics?.decode_tps_mean}<span
                >{t("平均")}
                {live.metrics.decode_tps_mean.toFixed(1)} tok/s</span
              >{/if}<button onclick={() => navigate("monitor")}
              >{t("查看监控")}</button
            >
          </div>{/if}
        <div class="quick-controls">
          <button
            onclick={() => (settingsOpen = true)}
            disabled={busy || ctx?.state !== "ready"}
            >{t("思考")}：{value("enable_thinking") == null
              ? t("未报告")
              : value("enable_thinking")
                ? t("开启")
                : t(
                    "关闭",
                  )}{#if value("enable_thinking") && value("reasoning_effort")}
              · {value("reasoning_effort")}{/if}</button
          >{#if value("reasoning_budget_tokens") != null}<span
              >{t("预算")}
              {value("reasoning_budget_tokens") === -1
                ? t("不限（-1）")
                : value("reasoning_budget_tokens")}</span
            >{/if}{#if Object.keys(overrides).length}<span class="changed"
              >{t("本对话已修改")} {Object.keys(overrides).length}</span
            >{/if}
        </div>
        <form
          onsubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          {#if images.length}<div class="attachments">
              {#each images as im, i}<button
                  type="button"
                  onclick={() => (images = images.filter((_, j) => i !== j))}
                  >{im.name} ×</button
                >{/each}
            </div>{/if}<textarea
            aria-label={t("消息输入")}
            bind:value={input}
            oninput={() => set(draftKey(), input)}
            disabled={loading || busy}
            placeholder={t("输入消息，Enter 发送，Shift+Enter 换行")}
            rows="2"
            onkeydown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
                e.preventDefault();
                void send();
              }
            }}
            onpaste={(e) => {
              const f = Array.from(e.clipboardData?.files || []);
              if (f.length && ctx?.capabilities?.vision) {
                e.preventDefault();
                void addImages(f);
              }
            }}
          ></textarea>
          <div class="compose-actions">
            <div>
              {#if ctx?.capabilities?.vision}<label class="button"
                  >＋ {t("图片")}<input
                    type="file"
                    accept="image/png,image/jpeg,image/webp,image/gif"
                    multiple
                    disabled={busy}
                    onchange={(e) => {
                      void addImages(Array.from(e.currentTarget.files || []));
                      e.currentTarget.value = "";
                    }}
                  /></label
                >{/if}{#if active}<button
                  type="button"
                  class="muted"
                  disabled={busy}
                  onclick={remove}>{t("删除对话")}</button
                >{/if}
            </div>
            <div>
              {#if busy}<button type="button" class="stop" onclick={stop}
                  >■ {t("停止生成")}</button
                >{:else}{#if messages.at(-1)?.finishReason === "length" || messages.at(-1)?.uiStatus === "interrupted"}<button
                    type="button"
                    onclick={() => send(true)}>{t("续写（新请求）")}</button
                  >{/if}<button
                  type="submit"
                  class="s-primary"
                  disabled={loading ||
                    ctx?.state !== "ready" ||
                    (!input.trim() && !images.length)}>{t("发送")} ↑</button
                >{/if}
            </div>
          </div>
        </form>
      </div>
    </section>
  </div>
  {#if settingsOpen}<div
      class="drawer-backdrop"
      role="presentation"
      onclick={(e) => {
        if (e.target === e.currentTarget) settingsOpen = false;
      }}
    ></div>
    <div
      class="settings-drawer"
      use:drawer
      tabindex="-1"
      role="dialog"
      aria-label={t("生成参数")}
      aria-modal="true"
    >
      <header>
        <strong>{t("生成与思考")}</strong><button
          onclick={() => (settingsOpen = false)}
          aria-label={t("关闭面板")}>×</button
        >
      </header>
      <div class="settings-body">
        {#if error}<div class="chat-error" role="alert">{error}</div>{/if}
        <p>{t("修改仅用于当前对话，下一次发送生效。")}</p>
        <label
          >{t("思考")}<small>{source("enable_thinking")}</small><select
            aria-label={t("思考")}
            value={value("enable_thinking") == null
              ? "unknown"
              : String(value("enable_thinking"))}
            onchange={(e) =>
              edit("enable_thinking", e.currentTarget.value === "true")}
            ><option value="unknown" disabled>{t("未报告")}</option><option
              value="true">{t("开启")}</option
            ><option value="false">{t("关闭")}</option></select
          ></label
        >
        {#if ctx?.capabilities?.efforts?.length}<label
            >{t("思考强度")}<small>{source("reasoning_effort")}</small><select
              aria-label={t("思考强度")}
              disabled={value("enable_thinking") === false}
              value={value("reasoning_effort") || ""}
              onchange={(e) => edit("reasoning_effort", e.currentTarget.value)}
              ><option value="" disabled>{t("未报告")}</option
              >{#if value("reasoning_effort") === "none"}<option value="none"
                  >none</option
                >{/if}{#each ctx.capabilities.efforts as effort}<option
                  value={effort}>{effort}</option
                >{/each}</select
            ></label
          >{/if}
        {#each [{ key: "reasoning_budget_tokens", label: "思考预算 · tokens", min: ctx?.backend === "llamacpp" ? -1 : (ctx?.budget_min ?? 0) }, { key: "max_tokens", label: "总输出上限 · tokens", min: 1 }] as f}<label
            >{t(f.label)}<small>{source(f.key)}</small><input
              aria-label={t(f.label)}
              type="number"
              min={f.min}
              max={ctx?.limit}
              step="1"
              value={value(f.key) ?? ""}
              placeholder={t("未报告")}
              onchange={(e) => editNumber(f.key, e.currentTarget)}
            /></label
          >{/each}<small
          >{t(
            ctx?.backend === "ninfer"
              ? "思考预算必须为正整数；总输出包含思考和正文。"
              : ctx?.backend === "polystrata"
                ? "思考预算 0 表示不设思考预算；总输出仍限制思考和正文。"
                : "思考预算 0 会立即结束思考；-1 恢复服务设置。",
          )}</small
        >
        {#if ctx?.capabilities?.preserve}<label class="check"
            ><input
              type="checkbox"
              checked={value("preserve_thinking") === true}
              onchange={(e) =>
                edit("preserve_thinking", e.currentTarget.checked)}
            />{t("保留历史思考")}<small
              >{value("preserve_thinking") == null
                ? t("未报告")
                : source("preserve_thinking")}</small
            ></label
          ><small
            >{t(
              "保留历史思考可能增加上下文占用；不影响思考内容的展开或折叠。",
            )}</small
          >{/if}
        <details>
          <summary>{t("采样参数")}</summary
          >{#each ["temperature", "top_p", "top_k", "min_p", "seed"] as key}<label
              >{key}<small>{source(key)}</small><input
                aria-label={key}
                type="number"
                step={["seed", "top_k"].includes(key) ? 1 : 0.05}
                value={shown(key) ?? ""}
                placeholder={t("未报告")}
                onchange={(e) => editNumber(key, e.currentTarget)}
              /></label
            >{/each}
        </details>
      </div>
      <footer>
        <button onclick={reset}>{t("恢复服务设置")}</button><button
          class="s-primary"
          onclick={() => (settingsOpen = false)}>{t("完成")}</button
        >
      </footer>
    </div>{/if}
</div>

<style>
  :global(html),
  :global(body) {
    height: 100%;
    overflow: hidden;
    margin: 0;
  }
  #app {
    height: 100dvh;
    color: var(--s-ink);
    background: var(--s-bg);
    /* Upstream Markdown widgets inherit the console palette too. */
    --background: var(--s-bg);
    --foreground: var(--s-ink);
    --card: var(--s-surface);
    --card-foreground: var(--s-ink);
    --muted: var(--s-soft);
    --muted-foreground: var(--s-muted);
    --primary: var(--s-info-text);
    --accent: var(--s-soft);
    --accent-foreground: var(--s-ink);
    --border: var(--s-line);
    --ring: var(--s-info);
    --code-background: var(--s-surface);
    --code-foreground: var(--s-ink);
  }
  .chat-shell {
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    height: 100%;
    font-family: var(--s-font);
  }
  button,
  .button,
  select,
  input,
  textarea {
    font: inherit;
    color: var(--s-ink);
    border: 1px solid var(--s-line);
    background: var(--s-soft);
    border-radius: 8px;
  }
  button,
  .button {
    padding: 6px 12px;
    min-height: 34px;
    background: transparent;
    border-radius: var(--s-button-radius);
    font-weight: var(--s-button-weight);
    cursor: pointer;
    font-size: 13px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
  }
  button:hover:not(:disabled),
  .button:hover:not(:has(input:disabled)) {
    background: var(--s-button-hover);
  }
  button:disabled,
  .button:has(input:disabled) {
    opacity: 0.45;
    cursor: default;
  }
  button:focus-visible,
  input:focus-visible,
  select:focus-visible,
  textarea:focus-visible,
  summary:focus-visible {
    outline: 2px solid var(--s-info);
    outline-offset: 2px;
  }
  .new-chat {
    min-height: 38px;
  }
  small,
  .muted {
    color: var(--s-muted);
  }
  a {
    color: var(--s-info);
  }
  aside {
    background: var(--s-surface);
    border-right: 1px solid var(--s-line);
    padding: 18px 14px;
    display: flex;
    flex-direction: column;
    gap: 18px;
    min-height: 0;
  }
  .side-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 14px;
  }
  aside nav {
    overflow: auto;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  aside nav button {
    display: block;
    text-align: left;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    background: transparent;
    border-color: transparent;
    min-height: 38px;
    flex-shrink: 0;
  }
  aside nav button.selected {
    background: var(--s-info-tint);
    color: var(--s-info-text);
    border-color: var(--s-info);
    font-weight: 600;
  }
  aside nav button:not(.selected):hover:not(:disabled) {
    background: var(--s-soft);
    border-color: var(--s-line);
  }
  .history-actions {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .history-actions small {
    font-size: 11px;
    text-align: center;
  }
  .chat-main {
    display: flex;
    flex-direction: column;
    min-width: 0;
    min-height: 0;
  }
  .chat-header {
    display: flex;
    gap: 14px;
    align-items: center;
    min-height: 72px;
    border-bottom: 1px solid var(--s-line);
    padding: 12px 24px;
    background: var(--s-surface);
  }
  .chat-header > div {
    flex: 1;
    min-width: 0;
  }
  .chat-header strong {
    font-size: 14px;
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .chat-header small {
    font-size: 12px;
  }
  .chat-scroll {
    overflow: auto;
    flex: 1;
    min-height: 0;
    padding: 20px 30px;
    scrollbar-gutter: stable;
  }
  .messages,
  .welcome,
  .migration {
    max-width: 900px;
    margin: auto;
  }
  .welcome {
    padding: 10vh 0;
    text-align: center;
  }
  .welcome h1 {
    font-size: 26px;
    font-weight: 650;
  }
  .welcome p {
    font-size: 14px;
    color: var(--s-muted);
    margin: 12px 0 24px;
  }
  article {
    margin: 0 0 24px;
    padding: 12px 16px;
    overflow-wrap: anywhere;
    min-width: 0;
  }
  article.user {
    background: var(--s-soft);
    border: 1px solid var(--s-line-soft);
    border-radius: 14px;
    margin-left: 8%;
  }
  .role {
    font-size: 12px;
    color: var(--s-muted);
    margin-bottom: 8px;
  }
  .user-text,
  .reasoning {
    white-space: pre-wrap;
  }
  .reasoning {
    font-size: 13px;
    line-height: 1.65;
    color: var(--s-muted);
    max-height: 340px;
    overflow: auto;
    padding: 0 12px 12px;
    margin: 0 12px 12px;
    border-left: 2px solid var(--s-info);
  }
  .thinking {
    margin-bottom: 10px;
    border: 1px solid var(--s-line);
    border-radius: 8px;
    background: var(--s-soft);
  }
  .thinking summary {
    font-size: 13px;
    color: var(--s-muted);
    cursor: pointer;
    padding: 10px 12px;
  }
  .attachment {
    max-width: min(320px, 100%);
    max-height: 240px;
    border-radius: 8px;
    margin: 8px 0;
  }
  .message-footer {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
    margin-top: 12px;
    font-size: 12px;
    color: var(--s-muted);
  }
  #app .message-footer button {
    border: 0;
    background: transparent;
    color: var(--s-muted);
    font-size: 12px;
    min-height: 28px;
    padding: 3px 6px;
  }
  #app .message-footer button:hover {
    background: var(--s-soft);
    color: var(--s-ink);
  }
  .message-footer details {
    flex-basis: 100%;
  }
  .message-details {
    padding: 10px;
    line-height: 1.8;
    background: var(--s-soft);
    overflow-wrap: anywhere;
    border: 1px solid var(--s-line);
    border-radius: 8px;
    margin-top: 8px;
  }
  summary {
    cursor: pointer;
  }
  .composer-wrap {
    padding: 8px 28px 18px;
    max-width: 1000px;
    width: 100%;
    margin: auto;
  }
  .quick-controls {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 0 0 8px;
    font-size: 12px;
    color: var(--s-muted);
  }
  .quick-controls button {
    font-size: 12px;
    padding: 5px 9px;
  }
  .changed {
    color: var(--s-info-text);
  }
  form {
    border: 1px solid var(--s-line);
    border-radius: 14px;
    background: var(--s-surface);
    box-shadow: var(--s-shadow);
    overflow: hidden;
  }
  form:focus-within {
    border-color: var(--s-info);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--s-info) 15%, transparent);
  }
  textarea {
    resize: none;
    display: block;
    width: 100%;
    min-height: 72px;
    max-height: 180px;
    padding: 14px 16px;
    border: 0;
    background: transparent;
    font-size: 14px;
    line-height: 1.55;
  }
  /* The composer owns the focus indicator; reset the forms plugin's inner ring. */
  #app textarea:focus {
    outline: 0;
    box-shadow: none;
  }
  textarea::placeholder {
    color: var(--s-muted);
  }
  .compose-actions {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 12px;
  }
  .compose-actions > div {
    display: flex;
    gap: 8px;
  }
  .button input {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    border: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  #app .button:has(input:focus-visible) {
    outline: 2px solid var(--s-info);
    outline-offset: 2px;
  }
  #app .button input:focus-visible {
    outline: 0;
  }
  #app button.stop {
    color: var(--s-error-text);
    border-color: var(--s-error-text);
    background: var(--s-error-tint);
  }
  #app button.muted {
    color: var(--s-muted);
  }
  .chat-error,
  .chat-notice {
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 13px;
    margin-bottom: 8px;
    overflow-wrap: anywhere;
  }
  .chat-error {
    background: var(--s-error-tint);
    color: var(--s-error-text);
  }
  .chat-notice {
    background: var(--s-info-tint);
    color: var(--s-info-text);
    display: flex;
    justify-content: space-between;
  }
  .chat-notice button {
    padding: 0 6px;
    background: transparent;
    border: 0;
  }
  .live-line {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    color: var(--s-muted);
    margin-bottom: 8px;
    flex-wrap: wrap;
  }
  .live-line button {
    margin-left: auto;
    font-size: 11px;
    padding: 3px 6px;
  }
  .pulse {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--s-accent);
  }
  .attachments {
    padding: 8px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .migration {
    margin-bottom: 20px !important;
    font-size: 13px;
  }
  .migration p {
    margin: 10px 0;
  }
  .row {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 10px;
  }
  .drawer-backdrop {
    position: fixed;
    inset: 0;
    background: #0005;
    z-index: 20;
  }
  #app .history-backdrop {
    position: fixed;
    inset: 0;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: #0006;
    z-index: 24;
  }
  .settings-drawer {
    position: fixed;
    right: 0;
    top: 0;
    bottom: 0;
    width: min(390px, 100%);
    background: var(--s-surface);
    border-left: 1px solid var(--s-line);
    display: flex;
    flex-direction: column;
    z-index: 21;
    box-shadow: var(--s-shadow);
  }
  .settings-drawer header,
  .settings-drawer footer {
    padding: 18px;
    border-bottom: 1px solid var(--s-line);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .settings-drawer footer {
    border-top: 1px solid var(--s-line);
    border-bottom: 0;
  }
  .settings-body {
    flex: 1;
    min-height: 0;
    overflow: auto;
    padding: 20px;
  }
  .settings-body p {
    font-size: 13px;
    color: var(--s-muted);
    margin-bottom: 18px;
  }
  .settings-body label {
    display: block;
    font-size: 13px;
    margin: 0 0 18px;
  }
  .settings-body label small {
    float: right;
    font-size: 11px;
  }
  .settings-body input:not([type="checkbox"]),
  .settings-body select {
    display: block;
    width: 100%;
    padding: 9px 10px;
    margin-top: 7px;
  }
  .settings-body > small {
    display: block;
    font-size: 11px;
    margin: -8px 0 18px;
    line-height: 1.7;
  }
  .settings-body .check {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  #app .settings-body .check input {
    appearance: none;
    position: relative;
    flex: none;
    width: 42px;
    height: 24px;
    margin: 0;
    padding: 0;
    border: 1px solid var(--s-muted);
    border-radius: 999px;
    background: var(--s-line);
    cursor: pointer;
  }
  #app .settings-body .check input::before {
    content: "";
    position: absolute;
    top: 3px;
    left: 3px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--s-muted);
  }
  #app .settings-body .check input:checked {
    background: var(--s-info);
    border-color: var(--s-info);
  }
  #app .settings-body .check input:checked::before {
    background: #fff;
    transform: translateX(18px);
  }
  .settings-body details summary {
    font-size: 13px;
    margin: 10px 0 20px;
  }
  .settings-body details {
    border-top: 1px solid var(--s-line);
    padding-top: 10px;
  }
  .mobile-only {
    display: none;
  }
  #app[data-density="compact"] aside {
    padding: 12px;
    gap: 12px;
  }
  #app[data-density="compact"] .chat-header {
    min-height: 58px;
    padding: 8px 20px;
  }
  #app[data-density="compact"] .chat-scroll {
    padding: 14px 24px;
  }
  #app[data-density="compact"] article {
    margin-bottom: 16px;
    padding: 10px 12px;
  }
  #app[data-density="compact"] .composer-wrap {
    padding-bottom: 12px;
  }
  :global(#app .chat-main pre) {
    max-width: 100%;
    overflow-x: auto;
  }
  :global(#app .markdown-content .code-block-wrapper) {
    border-color: var(--s-line);
    box-shadow: none;
  }
  :global(#app .markdown-content .code-block-header) {
    color: var(--s-muted);
    background: var(--s-soft);
    border-bottom: 1px solid var(--s-line);
  }
  :global(#app .markdown-content th) {
    background: var(--s-soft);
  }
  :global(#app .chat-main iframe) {
    max-width: 100%;
  }
  :global(#app .chat-main table) {
    display: table;
    max-width: 100%;
  }
  :global(#app .chat-main img) {
    max-width: 100%;
  }
  @media (max-width: 760px) {
    .chat-shell {
      grid-template-columns: 1fr;
    }
    aside {
      display: none;
    }
    aside.mobile-open {
      display: flex;
      position: absolute;
      inset: 0 16% 0 0;
      z-index: 25;
    }
    .mobile-only {
      display: inline-flex;
    }
    .chat-header {
      padding: 10px 12px;
      gap: 8px;
    }
    .chat-header .s-badge {
      display: none;
    }
    .chat-scroll {
      padding: 12px;
    }
    .composer-wrap {
      padding: 8px 12px 12px;
    }
    .quick-controls {
      gap: 6px;
      flex-wrap: wrap;
    }
    article {
      padding: 10px 0;
    }
    article.user {
      padding: 12px;
      margin-left: 3%;
    }
    .compose-actions button,
    .button {
      font-size: 12px;
      padding: 6px 8px;
    }
    #app[data-density="compact"] .chat-scroll {
      padding: 12px;
    }
    #app[data-density="compact"] .chat-header {
      padding: 8px 12px;
    }
  }
</style>
