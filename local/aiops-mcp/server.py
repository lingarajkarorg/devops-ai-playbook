"""
aiops-mcp: read-only AIOps tools for a Kubernetes cluster.

Exposes three tools to any MCP client (Claude Code, Copilot agent mode, ...):
  - fetch_health   -> Kubernetes API: pods, deployments, warning events, service wiring
  - fetch_logs     -> Loki (LogQL)
  - fetch_metrics  -> Prometheus (PromQL)

Everything is READ-ONLY and limited to the namespaces in ALLOWED_NAMESPACES.

Config (environment variables):
  PROM_URL            Prometheus base URL      (default http://localhost:9090)
  LOKI_URL            Loki base URL            (default http://localhost:3100)
  ALLOWED_NAMESPACES  comma-separated list     (default boutique)
  KUBECONFIG          standard kubeconfig path (in-cluster config is used if running in a pod)
"""

import os
import time
from datetime import datetime, timezone

import httpx
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.mcpserver import MCPServer

PROM_URL = os.environ.get("PROM_URL", "http://localhost:9090").rstrip("/")
LOKI_URL = os.environ.get("LOKI_URL", "http://localhost:3100").rstrip("/")
ALLOWED_NAMESPACES = [
    n.strip() for n in os.environ.get("ALLOWED_NAMESPACES", "boutique").split(",") if n.strip()
]
HTTP_TIMEOUT = 15.0
MAX_LOG_LINE = 500

mcp = MCPServer(
    "aiops",
    instructions=(
        "Read-only SRE tools for the kind cluster. Use fetch_health first to see what is "
        "broken, then fetch_logs for the failing app, then fetch_metrics to confirm. "
        f"Allowed namespaces: {', '.join(ALLOWED_NAMESPACES)}."
    ),
)

_k8s_loaded = False


def _k8s():
    """Load kube config once (in-cluster if available, else ~/.kube/config)."""
    global _k8s_loaded
    if not _k8s_loaded:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        _k8s_loaded = True
    return client.CoreV1Api(), client.AppsV1Api()


def _check_ns(namespace: str) -> str | None:
    if namespace not in ALLOWED_NAMESPACES:
        return f"Namespace '{namespace}' is not allowed. Allowed: {', '.join(ALLOWED_NAMESPACES)}"
    return None


def _age(ts) -> str:
    if ts is None:
        return "?"
    secs = int((datetime.now(timezone.utc) - ts).total_seconds())
    if secs < 120:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


@mcp.tool()
def fetch_health(namespace: str = "boutique") -> dict:
    """Health snapshot of a namespace from the Kubernetes API.

    Returns pods (status, readiness, restarts, last crash reason), deployments
    (desired vs ready replicas), recent Warning events, and a service-wiring check
    that flags Services whose targetPort is not a port any selected pod listens on.
    Call this first when investigating an incident.
    """
    if err := _check_ns(namespace):
        return {"error": err}
    try:
        core, apps = _k8s()
        pods = core.list_namespaced_pod(namespace).items
        deps = apps.list_namespaced_deployment(namespace).items
        svcs = core.list_namespaced_service(namespace).items
        events = core.list_namespaced_event(namespace).items
    except (ApiException, config.ConfigException, OSError) as e:
        return {"error": f"Kubernetes API error: {e}"}

    pod_rows = []
    for p in pods:
        statuses = p.status.container_statuses or []
        restarts = sum(c.restart_count for c in statuses)
        ready = sum(1 for c in statuses if c.ready)
        waiting = next(
            (c.state.waiting.reason for c in statuses if c.state and c.state.waiting), None
        )
        last_term = next(
            (
                f"{c.last_state.terminated.reason} (exit {c.last_state.terminated.exit_code})"
                for c in statuses
                if c.last_state and c.last_state.terminated
            ),
            None,
        )
        pod_rows.append(
            {
                "pod": p.metadata.name,
                "app": (p.metadata.labels or {}).get("app"),
                "phase": p.status.phase,
                "ready": f"{ready}/{len(statuses) or len(p.spec.containers)}",
                "restarts": restarts,
                "waiting_reason": waiting,
                "last_termination": last_term,
                "node": p.spec.node_name,
                "age": _age(p.metadata.creation_timestamp),
            }
        )

    dep_rows = [
        {
            "deployment": d.metadata.name,
            "desired": d.spec.replicas,
            "ready": d.status.ready_replicas or 0,
            "images": [c.image for c in d.spec.template.spec.containers],
        }
        for d in deps
    ]

    warnings = sorted(
        (e for e in events if e.type == "Warning"),
        key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp,
        reverse=True,
    )[:20]
    event_rows = [
        {
            "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "reason": e.reason,
            "message": (e.message or "")[:300],
            "count": e.count,
            "last_seen": _age(e.last_timestamp or e.event_time or e.metadata.creation_timestamp),
        }
        for e in warnings
    ]

    # Service wiring: does each Service's targetPort match a containerPort of its pods?
    wiring = []
    for s in svcs:
        selector = s.spec.selector or {}
        if not selector:
            continue
        matched = [
            p for p in pods
            if all((p.metadata.labels or {}).get(k) == v for k, v in selector.items())
        ]
        listening = {
            port.container_port
            for p in matched
            for c in p.spec.containers
            for port in (c.ports or [])
        }
        for sp in s.spec.ports or []:
            target = sp.target_port if sp.target_port is not None else sp.port
            ok = (not isinstance(target, int)) or (target in listening) or not listening
            wiring.append(
                {
                    "service": s.metadata.name,
                    "port": sp.port,
                    "target_port": target,
                    "pods_matched": len(matched),
                    "pod_container_ports": sorted(listening),
                    "status": "OK" if ok and matched else ("NO_PODS" if not matched else "PORT_MISMATCH"),
                }
            )

    unhealthy = [r for r in pod_rows if r["phase"] not in ("Running", "Succeeded") or r["waiting_reason"]]
    return {
        "namespace": namespace,
        "summary": {
            "pods": len(pod_rows),
            "unhealthy_pods": len(unhealthy),
            "warning_events": len(event_rows),
            "service_wiring_problems": sum(1 for w in wiring if w["status"] != "OK"),
        },
        "pods": pod_rows,
        "deployments": dep_rows,
        "warning_events": event_rows,
        "service_wiring": wiring,
    }


@mcp.tool()
def fetch_logs(
    namespace: str = "boutique",
    app: str | None = None,
    contains: str | None = None,
    since_minutes: int = 15,
    limit: int = 100,
) -> dict:
    """Recent log lines from Loki.

    Args:
        namespace: Kubernetes namespace (must be allowed).
        app: value of the pod's `app` label, e.g. "auth" or "product-service". Omit for all apps.
        contains: case-insensitive regex filter, e.g. "error|fail|refused". Omit for all lines.
        since_minutes: how far back to look (1-1440).
        limit: max lines to return (1-500), newest first.
    Logs from crashed/restarted containers are included because Loki keeps them.
    """
    if err := _check_ns(namespace):
        return {"error": err}
    since_minutes = max(1, min(int(since_minutes), 1440))
    limit = max(1, min(int(limit), 500))

    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"')

    selector = f'namespace="{esc(namespace)}"'
    if app:
        selector += f', app="{esc(app)}"'
    logql = "{" + selector + "}"
    if contains:
        logql += f' |~ "(?i){esc(contains)}"'

    now_ns = int(time.time() * 1e9)
    params = {
        "query": logql,
        "start": str(now_ns - since_minutes * 60 * 1_000_000_000),
        "end": str(now_ns),
        "limit": str(limit),
        "direction": "backward",
    }
    try:
        r = httpx.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        return {"error": f"Loki query failed ({LOKI_URL}): {e}", "logql": logql}

    lines = []
    for stream in data.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts, line in stream.get("values", []):
            lines.append(
                {
                    "time": datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).strftime("%H:%M:%S"),
                    "pod": labels.get("pod"),
                    "line": line[:MAX_LOG_LINE],
                    "_ts": int(ts),
                }
            )
    lines.sort(key=lambda x: x["_ts"], reverse=True)
    for line in lines:
        del line["_ts"]
    return {"logql": logql, "count": len(lines[:limit]), "lines": lines[:limit]}


@mcp.tool()
def fetch_metrics(promql: str, range_minutes: int = 0, step_seconds: int = 60) -> dict:
    """Run a PromQL query against Prometheus.

    Args:
        promql: the query, e.g. 'up{namespace="boutique"}' or
                'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="boutique"}[5m]))'.
        range_minutes: 0 for an instant query (current value); >0 for a time series over that window.
        step_seconds: resolution for range queries.
    Useful checks: which targets are scraped (up), restarts
    (kube_pod_container_status_restarts_total), memory (container_memory_working_set_bytes).
    """
    try:
        if range_minutes and range_minutes > 0:
            end = time.time()
            r = httpx.get(
                f"{PROM_URL}/api/v1/query_range",
                params={
                    "query": promql,
                    "start": end - int(range_minutes) * 60,
                    "end": end,
                    "step": max(15, int(step_seconds)),
                },
                timeout=HTTP_TIMEOUT,
            )
        else:
            r = httpx.get(f"{PROM_URL}/api/v1/query", params={"query": promql}, timeout=HTTP_TIMEOUT)
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"error": f"Prometheus query failed ({PROM_URL}): {e}", "promql": promql}

    if body.get("status") != "success":
        return {"error": body.get("error", "unknown error"), "promql": promql}

    out = []
    for series in body["data"]["result"][:50]:
        item = {"labels": series.get("metric", {})}
        if "value" in series:
            item["value"] = series["value"][1]
        else:
            vals = series.get("values", [])
            item["points"] = len(vals)
            item["first"] = vals[0][1] if vals else None
            item["last"] = vals[-1][1] if vals else None
            nums = [float(v[1]) for v in vals if v[1] not in ("NaN", "+Inf", "-Inf")]
            item["min"] = min(nums) if nums else None
            item["max"] = max(nums) if nums else None
        out.append(item)
    return {"promql": promql, "series": len(body["data"]["result"]), "results": out}


if __name__ == "__main__":
    mcp.run()  # stdio transport, what Claude Code uses