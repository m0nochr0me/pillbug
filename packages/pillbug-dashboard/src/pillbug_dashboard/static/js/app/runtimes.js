(function () {
  const initialDashboard =
    window.__PILLBUG_DASHBOARD_INITIAL__ &&
    typeof window.__PILLBUG_DASHBOARD_INITIAL__ === "object"
      ? window.__PILLBUG_DASHBOARD_INITIAL__
      : { summary: {}, runtimes: [] };

  const mountTarget = document.getElementById("runtimes-app");
  const vueApi = window.Vue;

  if (!mountTarget || !vueApi || typeof vueApi.createApp !== "function") {
    return;
  }

  /**
   * Build connected-component clusters from the A2A peer graph.
   * Each cluster groups runtimes that share direct or transitive A2A connections.
   * Runtimes with no peers are collected into standalone groups.
   */
  function computeClusters(runtimes) {
    const idToRuntime = new Map();
    const adjacency = new Map();

    for (const rt of runtimes) {
      const id = rt.registration.runtime_id;
      idToRuntime.set(id, rt);
      if (!adjacency.has(id)) adjacency.set(id, new Set());

      const peers = Array.isArray(rt.a2a_peers) ? rt.a2a_peers : [];
      for (const peer of peers) {
        adjacency.get(id).add(peer);
        if (!adjacency.has(peer)) adjacency.set(peer, new Set());
        adjacency.get(peer).add(id);
      }
    }

    const visited = new Set();
    const components = [];

    for (const rt of runtimes) {
      const id = rt.registration.runtime_id;
      if (visited.has(id)) continue;

      const component = [];
      const queue = [id];
      visited.add(id);

      while (queue.length) {
        const current = queue.shift();
        component.push(current);
        for (const neighbor of adjacency.get(current) || []) {
          if (!visited.has(neighbor)) {
            visited.add(neighbor);
            queue.push(neighbor);
          }
        }
      }

      components.push(component);
    }

    let peerGroupIndex = 0;
    return components.map((ids) => {
      const clusterRuntimes = ids
        .filter((id) => idToRuntime.has(id))
        .map((id) => idToRuntime.get(id));

      const edges = [];
      for (const rt of clusterRuntimes) {
        const source = rt.registration.runtime_id;
        for (const peer of Array.isArray(rt.a2a_peers) ? rt.a2a_peers : []) {
          edges.push({ key: `${source}->${peer}`, source, target: peer });
        }
      }

      const hasConnections = edges.length > 0;
      peerGroupIndex += hasConnections ? 1 : 0;

      return {
        id: `cluster-${ids.join("-")}`,
        label: hasConnections ? `PEER GROUP ${peerGroupIndex}` : "STANDALONE",
        runtimes: clusterRuntimes,
        edges,
      };
    });
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
          showForm: false,
          form: {
            runtime_id: "",
            label: "",
            base_url: "",
            dashboard_bearer_token: "",
            clear_dashboard_bearer_token: false,
          },
          isEditing: false,
          refreshTimer: null,
          // Per-runtime realtime state
          eventStreams: {},
          eventBuffers: {},
          lastHeartbeat: {},
        };
      },
      computed: {
        summary() {
          return this.dashboard.summary || {};
        },
        runtimes() {
          return Array.isArray(this.dashboard.runtimes)
            ? this.dashboard.runtimes
            : [];
        },
        filteredRuntimes() {
          const needle = this.query.trim().toLowerCase();
          if (!needle) return this.runtimes;

          return this.runtimes.filter((rt) => {
            const reg = rt.registration || {};
            return [
              reg.runtime_id || "",
              reg.label || "",
              reg.base_url || "",
              ...(Array.isArray(rt.a2a_peers) ? rt.a2a_peers : []),
            ]
              .join(" ")
              .toLowerCase()
              .includes(needle);
          });
        },
        clusters() {
          return computeClusters(this.filteredRuntimes);
        },
      },
      watch: {
        runtimes: {
          handler(newRuntimes) {
            this.syncEventStreams(newRuntimes);
          },
          deep: true,
        },
      },
      methods: {
        async confirmAction(options) {
          if (
            !window.PillbugDashboardConfirm ||
            typeof window.PillbugDashboardConfirm.open !== "function"
          ) {
            return false;
          }
          return window.PillbugDashboardConfirm.open(options);
        },
        statusLabel(runtime) {
          if (runtime.status && runtime.status.healthy) return "OK";
          if (runtime.status && runtime.status.connected) return "DEG";
          return "OFF";
        },
        statusClass(runtime) {
          if (runtime.status && runtime.status.healthy) return "healthy";
          if (runtime.status && runtime.status.connected) return "degraded";
          return "offline";
        },
        heartbeatClass(runtime) {
          const id = runtime.registration.runtime_id;
          const last = this.lastHeartbeat[id];
          if (!last) {
            if (runtime.status && runtime.status.healthy) return "alive";
            if (runtime.status && runtime.status.connected) return "stale";
            return "dead";
          }
          const age = Date.now() - last;
          if (age < 30000) return "alive";
          if (age < 90000) return "stale";
          return "dead";
        },
        activeSessions(runtime) {
          return runtime.runtime &&
            typeof runtime.runtime.active_session_count === "number"
            ? runtime.runtime.active_session_count
            : 0;
        },
        totalTasks(runtime) {
          return runtime.tasks &&
            runtime.tasks.scheduler &&
            typeof runtime.tasks.scheduler.total_tasks === "number"
            ? runtime.tasks.scheduler.total_tasks
            : 0;
        },
        channelCount(runtime) {
          return runtime.channels &&
            Array.isArray(runtime.channels.channels)
            ? runtime.channels.channels.length
            : 0;
        },
        runtimeEvents(runtime) {
          return this.eventBuffers[runtime.registration.runtime_id] || [];
        },
        formatTime(iso) {
          if (!iso) return "";
          const d = new Date(iso);
          if (Number.isNaN(d.getTime())) return "";
          return d.toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          });
        },
        runtimeDetailHref(runtime) {
          const runtimeId = runtime.registration
            ? runtime.registration.runtime_id
            : "";
          return `/runtimes/${encodeURIComponent(runtimeId)}`;
        },
        editRuntime(runtime) {
          const reg = runtime.registration || {};
          this.form.runtime_id = reg.runtime_id || "";
          this.form.label = reg.label || "";
          this.form.base_url = reg.base_url || "";
          this.form.dashboard_bearer_token = "";
          this.form.clear_dashboard_bearer_token = false;
          this.isEditing = true;
          this.showForm = true;
          this.actionMessage = "";
        },
        resetForm() {
          this.form = {
            runtime_id: "",
            label: "",
            base_url: "",
            dashboard_bearer_token: "",
            clear_dashboard_bearer_token: false,
          };
          this.isEditing = false;
        },
        async refreshDashboard(showSpinner) {
          if (showSpinner) this.loading = true;
          this.errorMessage = "";

          try {
            const response = await fetch("/api/runtimes", {
              headers: { Accept: "application/json" },
            });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const payload = await response.json();
            this.dashboard =
              payload && typeof payload === "object"
                ? payload
                : { summary: {}, runtimes: [] };
          } catch (error) {
            this.errorMessage =
              error instanceof Error ? error.message : "Unknown error";
          } finally {
            if (showSpinner) this.loading = false;
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
            if (!response.ok)
              throw new Error(payload.detail || `HTTP ${response.status}`);

            this.actionMessage = payload.message || "Saved.";
            this.resetForm();
            this.showForm = false;
            await this.refreshDashboard(false);
          } catch (error) {
            this.errorMessage =
              error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.submitting = false;
          }
        },
        async deleteRuntime(runtime) {
          const reg = runtime.registration || {};
          if (!reg.runtime_id) return;

          const confirmed = await this.confirmAction({
            title: `Remove ${reg.runtime_id}?`,
            message:
              "Removes from local registry. Does not stop the remote process.",
            confirmLabel: "Remove",
            cancelLabel: "Cancel",
            tone: "danger",
          });
          if (!confirmed) return;

          this.deletingRuntimeId = reg.runtime_id;
          this.errorMessage = "";
          this.actionMessage = "";

          try {
            const response = await fetch(
              `/api/runtimes/${encodeURIComponent(reg.runtime_id)}`,
              { method: "DELETE", headers: { Accept: "application/json" } },
            );
            const payload = await response.json();
            if (!response.ok)
              throw new Error(payload.detail || `HTTP ${response.status}`);

            this.actionMessage = payload.message || "Removed.";
            await this.refreshDashboard(false);
          } catch (error) {
            this.errorMessage =
              error instanceof Error ? error.message : "Unknown error";
          } finally {
            this.deletingRuntimeId = "";
          }
        },
        // ── Per-runtime SSE event streams ──────────────────────
        syncEventStreams(runtimes) {
          const currentIds = new Set(
            runtimes.map((r) => r.registration.runtime_id),
          );

          // Close streams for removed runtimes
          for (const id of Object.keys(this.eventStreams)) {
            if (!currentIds.has(id)) {
              this.eventStreams[id].abort();
              delete this.eventStreams[id];
              delete this.eventBuffers[id];
              delete this.lastHeartbeat[id];
            }
          }

          // Open streams for connected runtimes that lack one
          for (const rt of runtimes) {
            const id = rt.registration.runtime_id;
            if (!this.eventStreams[id] && rt.status && rt.status.connected) {
              this.openEventStream(id);
            }
          }
        },
        async openEventStream(runtimeId) {
          const controller = new AbortController();
          this.eventStreams[runtimeId] = controller;

          if (!this.eventBuffers[runtimeId]) {
            this.eventBuffers[runtimeId] = [];
          }

          try {
            const response = await fetch(
              `/api/runtimes/${encodeURIComponent(runtimeId)}/events?replay=5`,
              {
                headers: { Accept: "text/event-stream" },
                signal: controller.signal,
              },
            );

            if (!response.ok || !response.body)
              throw new Error(`HTTP ${response.status}`);

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
              const { value, done } = await reader.read();
              if (done) break;

              buffer += decoder.decode(value, { stream: true });
              const chunks = buffer.split("\n\n");
              buffer = chunks.pop() || "";

              for (const chunk of chunks) {
                this.handleSseChunk(runtimeId, chunk);
              }
            }
          } catch (error) {
            if (controller.signal.aborted) return;

            // Reconnect after 5 seconds
            setTimeout(() => {
              if (this.eventStreams[runtimeId] === controller) {
                delete this.eventStreams[runtimeId];
                this.openEventStream(runtimeId);
              }
            }, 5000);
          }
        },
        handleSseChunk(runtimeId, block) {
          const lines = block.split(/\r?\n/);
          let eventType = "message";
          const dataLines = [];

          for (const line of lines) {
            if (!line || line.startsWith(":")) continue;
            if (line.startsWith("event:")) eventType = line.slice(6).trim();
            else if (line.startsWith("data:"))
              dataLines.push(line.slice(5).trimStart());
          }

          if (!dataLines.length) return;

          let payload;
          try {
            payload = JSON.parse(dataLines.join("\n"));
          } catch {
            return;
          }

          // Update heartbeat timestamp
          this.lastHeartbeat[runtimeId] = Date.now();

          // Snapshots and errors are handled by polling
          if (
            eventType === "runtime.snapshot" ||
            eventType === "dashboard.error"
          ) {
            return;
          }

          const entry = {
            id:
              payload.event_id ||
              `${eventType}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
            type: eventType.replace(/^runtime\./, ""),
            time: this.formatTime(
              payload.occurred_at || new Date().toISOString(),
            ),
          };

          if (!this.eventBuffers[runtimeId]) {
            this.eventBuffers[runtimeId] = [];
          }

          this.eventBuffers[runtimeId].unshift(entry);
          this.eventBuffers[runtimeId] = this.eventBuffers[runtimeId].slice(
            0,
            5,
          );
        },
        stopAllStreams() {
          for (const ctrl of Object.values(this.eventStreams)) {
            ctrl.abort();
          }
          this.eventStreams = {};
        },
      },
      mounted() {
        this.syncEventStreams(this.runtimes);
        this.refreshTimer = window.setInterval(() => {
          this.refreshDashboard(false);
        }, 15000);
      },
      beforeUnmount() {
        if (this.refreshTimer) window.clearInterval(this.refreshTimer);
        this.stopAllStreams();
      },
    })
    .mount(mountTarget);
})();
