(function () {
  const initialRuntime = window.__PILLBUG_DASHBOARD_RUNTIME__ && typeof window.__PILLBUG_DASHBOARD_RUNTIME__ === "object"
    ? window.__PILLBUG_DASHBOARD_RUNTIME__
    : null;

  const ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

  function generateUlid() {
    let now = Date.now();
    let time = "";
    for (let i = 9; i >= 0; i--) {
      time = ULID_ALPHABET[now % 32] + time;
      now = Math.floor(now / 32);
    }
    const rnd = new Uint8Array(16);
    (window.crypto || window.msCrypto).getRandomValues(rnd);
    let random = "";
    for (let i = 0; i < 16; i++) {
      random += ULID_ALPHABET[rnd[i] & 0x1f];
    }
    return time + random;
  }

  function safeLocalGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function safeLocalSet(key, value) {
    try {
      if (value === null || value === undefined || value === "") {
        window.localStorage.removeItem(key);
      } else {
        window.localStorage.setItem(key, value);
      }
    } catch {
      /* localStorage unavailable — token only lives for this tab */
    }
  }

  function emptyTaskForm() {
    return {
      task_id: null,
      name: "",
      prompt: "",
      schedule_type: "cron",
      cron_expression: "",
      delay_seconds: null,
      timezone_name: "",
      enabled: true,
      repeat: false,
      clean_session: true,
      goal_enabled: false,
      goal: {
        done_condition: "",
        validation_prompt: "",
        max_steps_per_run: null,
        max_cost_per_run_usd: null,
      },
    };
  }

  const mountTarget = document.getElementById("runtime-detail-app");
  const vueApi = window.Vue;

  if (!mountTarget || !vueApi || typeof vueApi.createApp !== "function" || !initialRuntime) {
    return;
  }

  const runtimeId = initialRuntime.registration.runtime_id;
  const tokenStorageKey = `pillbug:ws-token:${runtimeId}`;
  const urlStorageKey = `pillbug:ws-url:${runtimeId}`;

  vueApi
    .createApp({
      delimiters: ["[[", "]]"],
      data() {
        const storedToken = safeLocalGet(tokenStorageKey) || "";
        const storedUrl = safeLocalGet(urlStorageKey) || "";
        return {
          detail: initialRuntime,
          events: [],
          refreshLoading: false,
          pendingActionKey: "",
          actionMessage: "",
          errorMessage: "",
          refreshTimer: null,
          detailRefreshTimer: null,
          refreshInFlight: false,
          refreshQueued: false,
          streamController: null,
          reconnectTimer: null,
          quickSendOpen: false,
          messageForm: {
            channel: "",
            conversation_id: "",
            message: "",
          },
          taskFormOpen: false,
          taskFormMode: "create",
          taskFormBusy: false,
          taskFormError: "",
          taskForm: emptyTaskForm(),
          historyDrawerOpen: false,
          historyDrawerSession: null,
          historyPreview: null,
          historyLoading: false,
          historyError: "",
          wsToken: storedToken,
          wsConnectUrl: storedUrl,
          wsTokenInput: "",
          wsUrlInput: storedUrl,
          chatStatus: "idle",
          chatError: "",
          chatConversationId: "",
          chatMessages: [],
          chatComposer: "",
          chatHistoryLoading: false,
        };
      },
      computed: {
        runtimeId() {
          return this.detail.registration.runtime_id;
        },
        agentCardHref() {
          return `/api/runtimes/${encodeURIComponent(this.runtimeId)}/agent-card`;
        },
        sessions() {
          return this.detail.sessions && Array.isArray(this.detail.sessions.sessions)
            ? this.detail.sessions.sessions
            : [];
        },
        tasks() {
          return this.detail.tasks && Array.isArray(this.detail.tasks.tasks)
            ? this.detail.tasks.tasks
            : [];
        },
        scheduler() {
          return this.detail.tasks && this.detail.tasks.scheduler
            ? this.detail.tasks.scheduler
            : {};
        },
        availableChannels() {
          const fromChannels = this.detail.channels && Array.isArray(this.detail.channels.enabled_channels)
            ? this.detail.channels.enabled_channels
            : [];
          const fromRuntime = this.detail.runtime && this.detail.runtime.metadata && Array.isArray(this.detail.runtime.metadata.enabled_channels)
            ? this.detail.runtime.metadata.enabled_channels
            : [];
          return Array.from(new Set([...fromChannels, ...fromRuntime])).filter(Boolean);
        },
        outboundDrafts() {
          return this.detail.drafts && Array.isArray(this.detail.drafts.outbound)
            ? this.detail.drafts.outbound
            : [];
        },
        commandDrafts() {
          return this.detail.drafts && Array.isArray(this.detail.drafts.command)
            ? this.detail.drafts.command
            : [];
        },
        hasDrafts() {
          return this.outboundDrafts.length > 0 || this.commandDrafts.length > 0;
        },
        chatAvailable() {
          return this.availableChannels.includes("websocket");
        },
        websocketChannelDetails() {
          const channels = this.detail.channels && Array.isArray(this.detail.channels.channels)
            ? this.detail.channels.channels
            : [];
          const entry = channels.find((c) => c && c.name === "websocket");
          return entry && entry.details ? entry.details : null;
        },
        defaultWsUrl() {
          const details = this.websocketChannelDetails;
          if (!details) {
            return "";
          }
          const port = details.port;
          if (!port) {
            return "";
          }
          try {
            const base = new URL(this.detail.registration.base_url);
            return `${base.protocol}//${base.hostname}:${port}`;
          } catch {
            return `http://${details.host || "127.0.0.1"}:${port}`;
          }
        },
        wsSocketioPath() {
          const details = this.websocketChannelDetails;
          if (details && typeof details.socketio_path === "string" && details.socketio_path) {
            return details.socketio_path;
          }
          return "/socket.io";
        },
        hasWsCredentials() {
          return Boolean(this.wsToken && this.wsConnectUrl);
        },
        chatConversationOptions() {
          const sessionOptions = this.sessions
            .filter((s) => s.channel_name === "websocket" && s.conversation_id)
            .map((s) => ({
              value: s.conversation_id,
              label: `${this.truncate(s.conversation_id, 12)} · msg ${s.message_count}`,
            }));
          if (
            this.chatConversationId
            && !sessionOptions.some((opt) => opt.value === this.chatConversationId)
          ) {
            sessionOptions.unshift({
              value: this.chatConversationId,
              label: `${this.truncate(this.chatConversationId, 12)} · NEW`,
            });
          }
          return sessionOptions;
        },
        chatStatusLabel() {
          const labels = {
            idle: "IDLE",
            connecting: "CONNECTING",
            live: "LIVE",
            disconnected: "DISCONNECTED",
            "auth-failed": "AUTH FAILED",
            error: "ERROR",
          };
          return labels[this.chatStatus] || this.chatStatus.toUpperCase();
        },
        reversedChatMessages() {
          return this.chatMessages.slice().reverse();
        },
        chatStatusTagClass() {
          if (this.chatStatus === "live") {
            return "primary";
          }
          if (this.chatStatus === "auth-failed" || this.chatStatus === "error") {
            return "secondary";
          }
          return "";
        },
      },
      methods: {
        async confirmAction(options) {
          if (!window.PillbugDashboardConfirm || typeof window.PillbugDashboardConfirm.open !== "function") {
            return false;
          }

          return window.PillbugDashboardConfirm.open(options);
        },
        truncate(value, max) {
          if (typeof value !== "string") {
            return "";
          }
          const limit = max || 80;
          if (value.length <= limit) {
            return value;
          }
          return `${value.slice(0, limit)}…`;
        },
        async runDraftDecision(actionKey, path, comment, successFallback) {
          this.pendingActionKey = actionKey;
          this.actionMessage = "";
          this.errorMessage = "";

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(this.runtimeId)}${path}`, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
              body: JSON.stringify(comment ? { comment } : {}),
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = payload.message || successFallback;
            await this.refreshDetail(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.pendingActionKey = "";
          }
        },
        async commitDraft(draft) {
          const decision = await this.confirmAction({
            title: `Commit ${draft.kind} draft?`,
            message: `Dispatch this outbound to ${draft.channel}${draft.target ? `:${draft.target}` : ""}.`,
            confirmLabel: "Commit & send",
            cancelLabel: "Cancel",
            withComment: true,
            commentLabel: "Operator note (optional)",
          });
          if (!decision) {
            return;
          }

          await this.runDraftDecision(
            `commit-${draft.id}`,
            `/control/drafts/${encodeURIComponent(draft.id)}/commit`,
            decision.comment,
            `Draft ${draft.id} committed.`,
          );
        },
        async discardDraft(draft) {
          const decision = await this.confirmAction({
            title: `Discard ${draft.kind} draft?`,
            message: `The outbound to ${draft.channel}${draft.target ? `:${draft.target}` : ""} will be dropped.`,
            confirmLabel: "Discard",
            cancelLabel: "Cancel",
            tone: "danger",
            withComment: true,
            commentLabel: "Reason (optional)",
          });
          if (!decision) {
            return;
          }

          await this.runDraftDecision(
            `discard-${draft.id}`,
            `/control/drafts/${encodeURIComponent(draft.id)}/discard`,
            decision.comment,
            `Draft ${draft.id} discarded.`,
          );
        },
        async approveCommand(draft) {
          const decision = await this.confirmAction({
            title: "Approve command?",
            message: draft.command,
            confirmLabel: "Approve",
            cancelLabel: "Cancel",
            withComment: true,
            commentLabel: "Operator note (optional)",
          });
          if (!decision) {
            return;
          }

          await this.runDraftDecision(
            `approve-${draft.id}`,
            `/control/approvals/${encodeURIComponent(draft.id)}/approve`,
            decision.comment,
            `Command draft ${draft.id} approved.`,
          );
        },
        async denyCommand(draft) {
          const decision = await this.confirmAction({
            title: "Deny command?",
            message: draft.command,
            confirmLabel: "Deny",
            cancelLabel: "Cancel",
            tone: "danger",
            withComment: true,
            commentLabel: "Reason (optional)",
          });
          if (!decision) {
            return;
          }

          await this.runDraftDecision(
            `deny-${draft.id}`,
            `/control/approvals/${encodeURIComponent(draft.id)}/deny`,
            decision.comment,
            `Command draft ${draft.id} denied.`,
          );
        },
        statusLabel(status) {
          if (status && status.healthy) {
            return "Healthy";
          }
          if (status && status.connected) {
            return "Degraded";
          }
          return "Offline";
        },
        statusClass(status) {
          if (status && status.healthy) {
            return "healthy";
          }
          if (status && status.connected) {
            return "degraded";
          }
          return "offline";
        },
        formatTimestamp(value) {
          if (!value) {
            return "Unknown";
          }

          const date = new Date(value);
          if (Number.isNaN(date.getTime())) {
            return "Unknown";
          }

          return date.toLocaleString();
        },
        scheduleDetailRefresh(delayMs) {
          if (this.detailRefreshTimer) {
            window.clearTimeout(this.detailRefreshTimer);
          }

          this.detailRefreshTimer = window.setTimeout(() => {
            this.detailRefreshTimer = null;
            this.refreshDetail(false);
          }, delayMs);
        },
        async refreshDetail(showSpinner) {
          if (this.refreshInFlight) {
            this.refreshQueued = true;
            if (showSpinner) {
              this.refreshLoading = true;
            }
            return;
          }

          if (showSpinner) {
            this.refreshLoading = true;
          }
          this.refreshInFlight = true;
          this.errorMessage = "";

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(this.runtimeId)}`, {
              headers: {
                Accept: "application/json",
              },
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.detail = payload;
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.refreshInFlight = false;
            if (showSpinner) {
              this.refreshLoading = false;
            }

            if (this.refreshQueued) {
              this.refreshQueued = false;
              this.scheduleDetailRefresh(0);
            }
          }
        },
        async sendMessage() {
          this.pendingActionKey = "send-message";
          this.actionMessage = "";
          this.errorMessage = "";

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(this.runtimeId)}/control/messages/send`, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
              body: JSON.stringify(this.messageForm),
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = payload.message || "Control message sent.";
            this.messageForm.message = "";
            await this.refreshDetail(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.pendingActionKey = "";
          }
        },
        async clearSession(session) {
          const confirmed = await this.confirmAction({
            title: `Clear ${session.session_key}?`,
            message: "Pending messages in the runtime buffer will be dropped for this tracked session.",
            confirmLabel: "Clear session",
            cancelLabel: "Cancel",
            tone: "danger",
          });
          if (!confirmed) {
            return;
          }

          const actionKey = `clear-${session.session_key}`;
          this.pendingActionKey = actionKey;
          this.actionMessage = "";
          this.errorMessage = "";

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(this.runtimeId)}/control/sessions/${encodeURIComponent(session.session_key)}/clear`, {
              method: "POST",
              headers: {
                Accept: "application/json",
              },
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = payload.message || `Cleared ${session.session_key}.`;
            await this.refreshDetail(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.pendingActionKey = "";
          }
        },
        openHistoryDrawer(session) {
          this.historyDrawerSession = session;
          this.historyDrawerOpen = true;
          this.historyPreview = null;
          this.historyError = "";
          this.fetchSessionHistory(session.session_key);
        },
        closeHistoryDrawer() {
          this.historyDrawerOpen = false;
          this.historyDrawerSession = null;
          this.historyPreview = null;
          this.historyError = "";
          this.historyLoading = false;
        },
        async refreshHistory() {
          if (!this.historyDrawerSession) {
            return;
          }
          await this.fetchSessionHistory(this.historyDrawerSession.session_key);
        },
        async fetchSessionHistory(sessionKey) {
          if (!sessionKey) {
            return;
          }
          this.historyLoading = true;
          this.historyError = "";

          try {
            const response = await fetch(
              `/api/runtimes/${encodeURIComponent(this.runtimeId)}/sessions/${encodeURIComponent(sessionKey)}/history`,
              { headers: { Accept: "application/json" } },
            );
            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            if (!this.historyDrawerOpen || !this.historyDrawerSession || this.historyDrawerSession.session_key !== sessionKey) {
              return;
            }
            this.historyPreview = payload;
          } catch (error) {
            this.historyError = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.historyLoading = false;
          }
        },
        saveWsCredentials() {
          const token = this.wsTokenInput.trim();
          if (!token) {
            return;
          }
          const url = (this.wsUrlInput || "").trim() || this.defaultWsUrl;
          if (!url) {
            this.chatError = "Set the websocket URL or wait for channel telemetry to load.";
            return;
          }
          this.wsToken = token;
          this.wsConnectUrl = url;
          safeLocalSet(tokenStorageKey, token);
          safeLocalSet(urlStorageKey, url);
          this.wsTokenInput = "";
          this.wsUrlInput = url;
          this.chatError = "";
          this.bootstrapChatConversation();
          this.connectChatSocket();
        },
        clearWsCredentials() {
          this.disconnectChatSocket();
          this.wsToken = "";
          this.wsConnectUrl = "";
          this.wsTokenInput = "";
          this.wsUrlInput = "";
          this.chatConversationId = "";
          this.chatMessages = [];
          this.chatError = "";
          this.chatStatus = "idle";
          safeLocalSet(tokenStorageKey, null);
          safeLocalSet(urlStorageKey, null);
        },
        bootstrapChatConversation() {
          if (this.chatConversationId) {
            return;
          }
          const existing = this.sessions.find((s) => s.channel_name === "websocket" && s.conversation_id);
          this.chatConversationId = existing ? existing.conversation_id : generateUlid();
        },
        startNewChatConversation() {
          this.disconnectChatSocket();
          this.chatConversationId = generateUlid();
          this.chatMessages = [];
          this.chatError = "";
          if (this.hasWsCredentials) {
            this.connectChatSocket();
          }
        },
        async onChatConversationChange() {
          this.disconnectChatSocket();
          this.chatMessages = [];
          this.chatError = "";
          if (!this.chatConversationId) {
            return;
          }
          await this.rehydrateChatTranscript();
          if (this.hasWsCredentials) {
            this.connectChatSocket();
          }
        },
        async rehydrateChatTranscript() {
          if (!this.chatConversationId) {
            return;
          }
          const sessionKey = `websocket:${this.chatConversationId}`;
          const tracked = this.sessions.find((s) => s.session_key === sessionKey);
          if (!tracked) {
            return;
          }
          this.chatHistoryLoading = true;
          try {
            const response = await fetch(
              `/api/runtimes/${encodeURIComponent(this.runtimeId)}/sessions/${encodeURIComponent(sessionKey)}/history`,
              { headers: { Accept: "application/json" } },
            );
            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            const turns = Array.isArray(payload.turns) ? payload.turns : [];
            this.chatMessages = turns
              .map((turn) => this.historyTurnToChatMessage(turn))
              .filter(Boolean);
          } catch (error) {
            this.chatError = `History fetch failed: ${error instanceof Error ? error.message : error}`;
          } finally {
            this.chatHistoryLoading = false;
          }
        },
        historyTurnToChatMessage(turn) {
          if (!turn || typeof turn.text !== "string" || !turn.text.trim()) {
            return null;
          }
          const role = turn.role === "user" ? "operator" : turn.role === "model" ? "runtime" : null;
          if (!role) {
            return null;
          }
          return { role, text: turn.text, at: turn.occurred_at || new Date().toISOString() };
        },
        connectChatSocket() {
          if (!this.chatAvailable || !this.hasWsCredentials) {
            return;
          }
          if (!this.chatConversationId || !ULID_RE.test(this.chatConversationId)) {
            this.chatError = "Conversation ID must be a 26-character ULID.";
            return;
          }
          if (typeof window.io !== "function") {
            this.chatError = "Socket.IO client failed to load.";
            return;
          }
          this.disconnectChatSocket();
          this.chatError = "";
          this.chatStatus = "connecting";

          let socket;
          try {
            socket = window.io(this.wsConnectUrl, {
              path: this.wsSocketioPath,
              transports: ["polling"],
              upgrade: false,
              reconnection: false,
              forceNew: true,
              autoConnect: false,
              extraHeaders: {
                Authorization: `Bearer ${this.wsToken}`,
                "X-SessionID": this.chatConversationId,
              },
            });
          } catch (error) {
            this.chatStatus = "error";
            this.chatError = `Socket setup failed: ${error instanceof Error ? error.message : error}`;
            return;
          }

          // Socket.IO's Socket carries internal state that does not survive a Vue
          // reactive Proxy wrap — keep it on a non-reactive instance property.
          this._chatSocket = socket;
          const sessionId = this.chatConversationId;

          socket.on("connect", () => {
            if (this._chatSocket !== socket || this.chatConversationId !== sessionId) {
              return;
            }
            this.chatStatus = "live";
            this.chatError = "";
          });

          socket.on("connect_error", (err) => {
            if (this._chatSocket !== socket) {
              return;
            }
            const message = err && err.message ? err.message : String(err);
            const looksAuth = /token|auth|forbidden|400/i.test(message);
            this.chatStatus = looksAuth ? "auth-failed" : "error";
            this.chatError = `Connection refused: ${message}`;
            this._chatSocket = null;
            try {
              socket.close();
            } catch {
              /* ignore */
            }
          });

          socket.on("disconnect", (reason) => {
            if (this._chatSocket !== socket) {
              return;
            }
            this._chatSocket = null;
            this.finalizeStreamingChatMessage();
            if (this.chatStatus === "live") {
              this.chatStatus = "disconnected";
              this.chatError = `Disconnected: ${reason}`;
            }
          });

          socket.on("stream", (payload) => {
            if (this._chatSocket !== socket || this.chatConversationId !== sessionId) {
              return;
            }
            const delta = payload && typeof payload.delta === "string" ? payload.delta : "";
            if (!delta) {
              return;
            }
            const last = this.chatMessages[this.chatMessages.length - 1];
            if (last && last.role === "runtime" && last.streaming) {
              last.text += delta;
            } else {
              this.chatMessages.push({ role: "runtime", text: delta, at: new Date().toISOString(), streaming: true });
            }
          });

          socket.on("message", (payload) => {
            if (this._chatSocket !== socket || this.chatConversationId !== sessionId) {
              return;
            }
            const text = payload && typeof payload.text === "string" ? payload.text : "";
            if (!text) {
              return;
            }
            const last = this.chatMessages[this.chatMessages.length - 1];
            if (last && last.role === "runtime" && last.streaming) {
              // A streamed response ends with the authoritative full-text `message`
              // event — replace the accumulated delta buffer instead of duplicating it.
              last.text = text;
              last.streaming = false;
              return;
            }
            this.chatMessages.push({ role: "runtime", text, at: new Date().toISOString() });
          });

          socket.connect();
        },
        finalizeStreamingChatMessage() {
          const last = this.chatMessages[this.chatMessages.length - 1];
          if (last && last.role === "runtime" && last.streaming) {
            last.streaming = false;
          }
        },
        disconnectChatSocket() {
          if (this._chatSocket) {
            try {
              this._chatSocket.close();
            } catch {
              /* ignore */
            }
            this._chatSocket = null;
          }
          this.finalizeStreamingChatMessage();
          if (this.chatStatus === "live" || this.chatStatus === "connecting") {
            this.chatStatus = "idle";
          }
        },
        sendChatMessage() {
          const text = (this.chatComposer || "").trim();
          if (!text) {
            return;
          }
          if (!this._chatSocket || this.chatStatus !== "live") {
            this.chatError = "Not connected.";
            return;
          }
          try {
            this._chatSocket.emit("message", { text });
          } catch (error) {
            this.chatError = `Send failed: ${error instanceof Error ? error.message : error}`;
            return;
          }
          this.chatMessages.push({ role: "operator", text, at: new Date().toISOString() });
          this.chatComposer = "";
        },
        async toggleTask(task, enable) {
          const action = enable ? "enable" : "disable";
          const actionKey = `${action}-${task.task_id}`;
          this.pendingActionKey = actionKey;
          this.actionMessage = "";
          this.errorMessage = "";

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(this.runtimeId)}/control/tasks/${encodeURIComponent(task.task_id)}/${action}`, {
              method: "POST",
              headers: {
                Accept: "application/json",
              },
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = payload.message || `Task ${action}d.`;
            await this.refreshDetail(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.pendingActionKey = "";
          }
        },
        openCreateTask() {
          this.taskForm = emptyTaskForm();
          this.taskFormMode = "create";
          this.taskFormError = "";
          this.taskFormOpen = true;
        },
        openEditTask(task) {
          const form = emptyTaskForm();
          form.task_id = task.task_id;
          form.name = task.name || "";
          form.prompt = task.prompt || "";
          form.enabled = Boolean(task.enabled);
          form.clean_session = task.clean_session !== false;

          const schedule = task.schedule || {};
          if (schedule.kind === "cron") {
            form.schedule_type = "cron";
            form.cron_expression = schedule.expression || "";
            form.timezone_name = schedule.timezone || "";
          } else {
            form.schedule_type = "delayed";
            form.delay_seconds = typeof schedule.delay_seconds === "number" ? schedule.delay_seconds : null;
            form.repeat = Boolean(schedule.repeat);
          }

          if (task.goal) {
            form.goal_enabled = true;
            form.goal.done_condition = task.goal.done_condition || "";
            form.goal.validation_prompt = task.goal.validation_prompt || "";
            form.goal.max_steps_per_run = typeof task.goal.max_steps_per_run === "number"
              ? task.goal.max_steps_per_run
              : null;
            form.goal.max_cost_per_run_usd = typeof task.goal.max_cost_per_run_usd === "number"
              ? task.goal.max_cost_per_run_usd
              : null;
          }

          this.taskForm = form;
          this.taskFormMode = "edit";
          this.taskFormError = "";
          this.taskFormOpen = true;
        },
        closeTaskForm() {
          if (this.taskFormBusy) {
            return;
          }
          this.taskFormOpen = false;
          this.taskFormError = "";
        },
        buildTaskPayload() {
          const f = this.taskForm;
          const payload = {
            name: f.name.trim(),
            prompt: f.prompt.trim(),
            schedule_type: f.schedule_type,
            enabled: f.enabled,
            clean_session: f.clean_session,
          };

          if (f.schedule_type === "cron") {
            payload.cron_expression = (f.cron_expression || "").trim();
            const tz = (f.timezone_name || "").trim();
            if (tz) {
              payload.timezone_name = tz;
            }
          } else {
            payload.delay_seconds = Number(f.delay_seconds);
            payload.repeat = Boolean(f.repeat);
          }

          if (f.goal_enabled) {
            const goal = {};
            const dc = (f.goal.done_condition || "").trim();
            const vp = (f.goal.validation_prompt || "").trim();
            if (dc) goal.done_condition = dc;
            if (vp) goal.validation_prompt = vp;
            if (f.goal.max_steps_per_run !== null && f.goal.max_steps_per_run !== "") {
              goal.max_steps_per_run = Number(f.goal.max_steps_per_run);
            }
            if (f.goal.max_cost_per_run_usd !== null && f.goal.max_cost_per_run_usd !== "") {
              goal.max_cost_per_run_usd = Number(f.goal.max_cost_per_run_usd);
            }
            if (Object.keys(goal).length) {
              payload.goal = goal;
            }
          } else if (this.taskFormMode === "edit") {
            payload.clear_goal = true;
          }

          return payload;
        },
        validateTaskForm(payload) {
          if (!payload.name) {
            return "Name is required.";
          }
          if (!payload.prompt) {
            return "Prompt is required.";
          }
          if (payload.schedule_type === "cron" && !payload.cron_expression) {
            return "Cron expression is required.";
          }
          if (payload.schedule_type === "delayed") {
            if (!Number.isFinite(payload.delay_seconds) || payload.delay_seconds < 1) {
              return "Delay (seconds) must be a positive integer.";
            }
          }
          return "";
        },
        async submitTaskForm() {
          if (this.taskFormBusy) {
            return;
          }
          const payload = this.buildTaskPayload();
          const validationError = this.validateTaskForm(payload);
          if (validationError) {
            this.taskFormError = validationError;
            return;
          }

          this.taskFormBusy = true;
          this.taskFormError = "";
          this.actionMessage = "";
          this.errorMessage = "";

          const isEdit = this.taskFormMode === "edit";
          const url = isEdit
            ? `/api/runtimes/${encodeURIComponent(this.runtimeId)}/control/tasks/${encodeURIComponent(this.taskForm.task_id)}`
            : `/api/runtimes/${encodeURIComponent(this.runtimeId)}/control/tasks`;
          const method = isEdit ? "PATCH" : "POST";

          try {
            const response = await fetch(url, {
              method,
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
              body: JSON.stringify(payload),
            });

            const responsePayload = await response.json();
            if (!response.ok) {
              throw new Error(responsePayload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = responsePayload.message || (isEdit ? "Task updated." : "Task created.");
            this.taskFormOpen = false;
            await this.refreshDetail(false);
          } catch (error) {
            this.taskFormError = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.taskFormBusy = false;
          }
        },
        async deleteTask(task) {
          const decision = await this.confirmAction({
            title: `Delete task ${task.name}?`,
            message: `Permanently removes scheduled task ${task.task_id}. This cannot be undone.`,
            confirmLabel: "Delete",
            cancelLabel: "Cancel",
            tone: "danger",
          });
          if (!decision) {
            return;
          }

          const actionKey = `delete-${task.task_id}`;
          this.pendingActionKey = actionKey;
          this.actionMessage = "";
          this.errorMessage = "";

          try {
            const response = await fetch(
              `/api/runtimes/${encodeURIComponent(this.runtimeId)}/control/tasks/${encodeURIComponent(task.task_id)}`,
              {
                method: "DELETE",
                headers: { Accept: "application/json" },
              },
            );
            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }
            this.actionMessage = payload.message || `Deleted ${task.task_id}.`;
            await this.refreshDetail(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.pendingActionKey = "";
          }
        },
        async runTask(task) {
          const actionKey = `run-${task.task_id}`;
          this.pendingActionKey = actionKey;
          this.actionMessage = "";
          this.errorMessage = "";

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(this.runtimeId)}/control/tasks/${encodeURIComponent(task.task_id)}/run-now`, {
              method: "POST",
              headers: {
                Accept: "application/json",
              },
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = payload.message || `Ran ${task.task_id}.`;
            await this.refreshDetail(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.pendingActionKey = "";
          }
        },
        async requestRuntimeAction(action) {
          const prompts = {
            drain: {
              title: "Request runtime drain?",
              message: "New work will stop after the current queue clears.",
              confirmLabel: "Request drain",
              cancelLabel: "Cancel",
            },
            shutdown: {
              title: "Request runtime shutdown?",
              message: "The remote process will begin stopping immediately.",
              confirmLabel: "Request shutdown",
              cancelLabel: "Cancel",
              tone: "danger",
            },
          };
          const confirmed = await this.confirmAction(prompts[action] || {
            title: "Proceed?",
            message: "Confirm this runtime action.",
            confirmLabel: "Confirm",
            cancelLabel: "Cancel",
          });
          if (!confirmed) {
            return;
          }

          this.pendingActionKey = action;
          this.actionMessage = "";
          this.errorMessage = "";

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(this.runtimeId)}/control/runtime/${action}`, {
              method: "POST",
              headers: {
                Accept: "application/json",
              },
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = payload.message || `Runtime ${action} requested.`;
            await this.refreshDetail(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.pendingActionKey = "";
          }
        },
        parseSseEvent(block) {
          const lines = block.split(/\r?\n/);
          let eventType = "message";
          const dataLines = [];

          for (const line of lines) {
            if (!line || line.startsWith(":")) {
              continue;
            }
            if (line.startsWith("event:")) {
              eventType = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
              dataLines.push(line.slice(5).trimStart());
            }
          }

          if (!dataLines.length) {
            return null;
          }

          try {
            return {
              eventType,
              payload: JSON.parse(dataLines.join("\n")),
            };
          } catch {
            return null;
          }
        },
        applyEvent(event) {
          if (!event) {
            return;
          }

          if (event.eventType === "dashboard.error") {
            this.errorMessage = event.payload.detail || "Runtime event stream failed.";
            return;
          }

          if (event.eventType === "runtime.snapshot") {
            this.detail.runtime = event.payload;
            return;
          }

          const entry = {
            event_id: event.payload.event_id || `${event.eventType}-${Date.now()}`,
            event_type: event.eventType,
            occurred_at: event.payload.occurred_at || new Date().toISOString(),
            level: event.payload.level || "info",
            message: event.payload.message || event.eventType,
            source: event.payload.source || "runtime",
            data: event.payload.data || {},
          };

          this.events.unshift(entry);
          this.events = this.events.slice(0, 120);
          this.scheduleDetailRefresh(250);
        },
        stopEventStream() {
          if (this.streamController) {
            this.streamController.abort();
            this.streamController = null;
          }

          if (this.detailRefreshTimer) {
            window.clearTimeout(this.detailRefreshTimer);
            this.detailRefreshTimer = null;
          }

          if (this.reconnectTimer) {
            window.clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
          }
        },
        async subscribeToEvents() {
          this.stopEventStream();
          const controller = new AbortController();
          this.streamController = controller;

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(this.runtimeId)}/events?replay=25`, {
              headers: {
                Accept: "text/event-stream",
              },
              signal: controller.signal,
            });

            if (!response.ok || !response.body) {
              throw new Error(`HTTP ${response.status}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
              const { value, done } = await reader.read();
              if (done) {
                break;
              }

              buffer += decoder.decode(value, { stream: true });
              const chunks = buffer.split("\n\n");
              buffer = chunks.pop() || "";

              for (const chunk of chunks) {
                this.applyEvent(this.parseSseEvent(chunk));
              }
            }
          } catch (error) {
            if (controller.signal.aborted) {
              return;
            }

            this.errorMessage = error instanceof Error ? error.message : "Event stream disconnected.";
            this.reconnectTimer = window.setTimeout(() => {
              this.subscribeToEvents();
            }, 3000);
          }
        },
      },
      mounted() {
        this.subscribeToEvents();
        this.refreshTimer = window.setInterval(() => {
          this.refreshDetail(false);
        }, 15000);
        this._handleKeydown = (event) => {
          if (event.key !== "Escape") {
            return;
          }
          if (this.historyDrawerOpen) {
            this.closeHistoryDrawer();
          }
        };
        window.addEventListener("keydown", this._handleKeydown);

        if (this.chatAvailable && this.hasWsCredentials) {
          this.bootstrapChatConversation();
          if (this.chatConversationId) {
            this.rehydrateChatTranscript().finally(() => this.connectChatSocket());
          }
        }
      },
      beforeUnmount() {
        if (this.refreshTimer) {
          window.clearInterval(this.refreshTimer);
        }
        this.stopEventStream();
        this.disconnectChatSocket();
        if (this._handleKeydown) {
          window.removeEventListener("keydown", this._handleKeydown);
          this._handleKeydown = null;
        }
      },
    })
    .mount(mountTarget);
})();
