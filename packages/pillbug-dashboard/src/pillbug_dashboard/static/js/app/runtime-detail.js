(function () {
  const initialRuntime = window.__PILLBUG_DASHBOARD_RUNTIME__ && typeof window.__PILLBUG_DASHBOARD_RUNTIME__ === "object"
    ? window.__PILLBUG_DASHBOARD_RUNTIME__
    : null;

  const mountTarget = document.getElementById("runtime-detail-app");
  const vueApi = window.Vue;

  if (!mountTarget || !vueApi || typeof vueApi.createApp !== "function" || !initialRuntime) {
    return;
  }

  vueApi
    .createApp({
      delimiters: ["[[", "]]"],
      data() {
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
          messageForm: {
            channel: "",
            conversation_id: "",
            message: "",
          },
        };
      },
      computed: {
        runtimeId() {
          return this.detail.registration.runtime_id;
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
      },
      methods: {
        async confirmAction(options) {
          if (!window.PillbugDashboardConfirm || typeof window.PillbugDashboardConfirm.open !== "function") {
            return false;
          }

          return window.PillbugDashboardConfirm.open(options);
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
        seedDefaultChannel() {
          if (!this.messageForm.channel && this.availableChannels.length) {
            const preferredChannel = this.availableChannels.find((channel) => channel !== "a2a") || this.availableChannels[0];
            this.messageForm.channel = preferredChannel || "";
          }
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
            this.seedDefaultChannel();
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
        this.seedDefaultChannel();
        this.subscribeToEvents();
        this.refreshTimer = window.setInterval(() => {
          this.refreshDetail(false);
        }, 15000);
      },
      beforeUnmount() {
        if (this.refreshTimer) {
          window.clearInterval(this.refreshTimer);
        }
        this.stopEventStream();
      },
    })
    .mount(mountTarget);
})();
