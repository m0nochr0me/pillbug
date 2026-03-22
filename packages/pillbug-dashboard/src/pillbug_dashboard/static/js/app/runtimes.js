(function () {
  const initialDashboard = window.__PILLBUG_DASHBOARD_INITIAL__ && typeof window.__PILLBUG_DASHBOARD_INITIAL__ === "object"
    ? window.__PILLBUG_DASHBOARD_INITIAL__
    : { summary: {}, runtimes: [] };
  const registryPath = typeof window.__PILLBUG_DASHBOARD_REGISTRY_PATH__ === "string"
    ? window.__PILLBUG_DASHBOARD_REGISTRY_PATH__
    : "";

  const mountTarget = document.getElementById("runtimes-app");
  const vueApi = window.Vue;

  if (!mountTarget || !vueApi || typeof vueApi.createApp !== "function") {
    return;
  }

  vueApi
    .createApp({
      delimiters: ["[[", "]]"],
      data() {
        return {
          dashboard: initialDashboard,
          query: "",
          loading: false,
          submitting: false,
          deletingRuntimeId: "",
          actionMessage: "",
          errorMessage: "",
          registryPath,
          form: {
            runtime_id: "",
            label: "",
            base_url: "",
            dashboard_bearer_token: "",
            clear_dashboard_bearer_token: false,
          },
          isEditing: false,
          refreshTimer: null,
        };
      },
      computed: {
        summary() {
          return this.dashboard.summary || {};
        },
        runtimes() {
          return Array.isArray(this.dashboard.runtimes) ? this.dashboard.runtimes : [];
        },
        filteredRuntimes() {
          const needle = this.query.trim().toLowerCase();
          if (!needle) {
            return this.runtimes;
          }

          return this.runtimes.filter((runtime) => {
            const registration = runtime.registration || {};
            return [
              registration.runtime_id || "",
              registration.label || "",
              registration.base_url || "",
              ...(Array.isArray(runtime.a2a_peers) ? runtime.a2a_peers : []),
            ]
              .join(" ")
              .toLowerCase()
              .includes(needle);
          });
        },
        filteredCount() {
          return this.filteredRuntimes.length;
        },
        topologyEdges() {
          return this.runtimes.flatMap((runtime) => {
            const peers = Array.isArray(runtime.a2a_peers) ? runtime.a2a_peers : [];
            const source = runtime.registration && runtime.registration.runtime_id
              ? runtime.registration.runtime_id
              : "unknown";
            return peers.map((peer) => ({
              key: `${source}->${peer}`,
              source,
              target: peer,
            }));
          });
        },
      },
      methods: {
        async confirmAction(options) {
          if (!window.PillbugDashboardConfirm || typeof window.PillbugDashboardConfirm.open !== "function") {
            return false;
          }

          return window.PillbugDashboardConfirm.open(options);
        },
        statusLabel(runtime) {
          if (runtime.status && runtime.status.healthy) {
            return "Healthy";
          }
          if (runtime.status && runtime.status.connected) {
            return "Degraded";
          }
          return "Offline";
        },
        statusClass(runtime) {
          if (runtime.status && runtime.status.healthy) {
            return "healthy";
          }
          if (runtime.status && runtime.status.connected) {
            return "degraded";
          }
          return "offline";
        },
        activeSessions(runtime) {
          return runtime.runtime && typeof runtime.runtime.active_session_count === "number"
            ? runtime.runtime.active_session_count
            : 0;
        },
        totalTasks(runtime) {
          return runtime.tasks && runtime.tasks.scheduler && typeof runtime.tasks.scheduler.total_tasks === "number"
            ? runtime.tasks.scheduler.total_tasks
            : 0;
        },
        channelCount(runtime) {
          return runtime.channels && Array.isArray(runtime.channels.channels)
            ? runtime.channels.channels.length
            : 0;
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
        runtimeDetailHref(runtime) {
          const runtimeId = runtime.registration ? runtime.registration.runtime_id : "";
          return `/runtimes/${encodeURIComponent(runtimeId)}`;
        },
        editRuntime(runtime) {
          const registration = runtime.registration || {};
          this.form.runtime_id = registration.runtime_id || "";
          this.form.label = registration.label || "";
          this.form.base_url = registration.base_url || "";
          this.form.dashboard_bearer_token = "";
          this.form.clear_dashboard_bearer_token = false;
          this.isEditing = true;
          this.actionMessage = "";
        },
        resetForm() {
          this.form.runtime_id = "";
          this.form.label = "";
          this.form.base_url = "";
          this.form.dashboard_bearer_token = "";
          this.form.clear_dashboard_bearer_token = false;
          this.isEditing = false;
        },
        async refreshDashboard(showSpinner) {
          if (showSpinner) {
            this.loading = true;
          }
          this.errorMessage = "";

          try {
            const response = await fetch("/api/runtimes", {
              headers: {
                Accept: "application/json",
              },
            });

            if (!response.ok) {
              throw new Error(`HTTP ${response.status}`);
            }

            const payload = await response.json();
            this.dashboard = payload && typeof payload === "object"
              ? payload
              : { summary: {}, runtimes: [] };
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            if (showSpinner) {
              this.loading = false;
            }
          }
        },
        async saveRuntime() {
          this.submitting = true;
          this.errorMessage = "";
          this.actionMessage = "";

          try {
            const response = await fetch("/api/runtimes", {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
              },
              body: JSON.stringify(this.form),
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = payload.message || "Runtime saved.";
            this.resetForm();
            await this.refreshDashboard(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.submitting = false;
          }
        },
        async deleteRuntime(runtime) {
          const registration = runtime.registration || {};
          if (!registration.runtime_id) {
            return;
          }

          const confirmed = await this.confirmAction({
            title: `Remove ${registration.runtime_id}?`,
            message: "This removes the runtime from the local dashboard registry. It does not stop the remote Pillbug process.",
            confirmLabel: "Remove",
            cancelLabel: "Cancel",
            tone: "danger",
          });
          if (!confirmed) {
            return;
          }

          this.deletingRuntimeId = registration.runtime_id;
          this.errorMessage = "";
          this.actionMessage = "";

          try {
            const response = await fetch(`/api/runtimes/${encodeURIComponent(registration.runtime_id)}`, {
              method: "DELETE",
              headers: {
                Accept: "application/json",
              },
            });

            const payload = await response.json();
            if (!response.ok) {
              throw new Error(payload.detail || `HTTP ${response.status}`);
            }

            this.actionMessage = payload.message || "Runtime removed.";
            await this.refreshDashboard(false);
          } catch (error) {
            this.errorMessage = error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.deletingRuntimeId = "";
          }
        },
      },
      mounted() {
        this.refreshTimer = window.setInterval(() => {
          this.refreshDashboard(false);
        }, 20000);
      },
      beforeUnmount() {
        if (this.refreshTimer) {
          window.clearInterval(this.refreshTimer);
        }
      },
    })
    .mount(mountTarget);
})();
