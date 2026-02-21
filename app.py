"""
Antigravity Quota Monitor — Flask backend
Detects the Antigravity Language Server process, proxies the GetUserStatus API
over HTTP/2, and serves a web dashboard showing model quota usage.
"""

import json
import logging
import platform
import re
import socket
import warnings
from datetime import datetime, timezone

import httpx
import psutil
from flask import Flask, jsonify, render_template

# ─── Configuration ─────────────────────────────────────────────────────────────

APP_PORT = 5050
LS_TIMEOUT = 30.0
LS_SERVICE = "exa.language_server_pb.LanguageServerService"

# Process name patterns per platform
_LS_PROCESS_NAMES = {
    "Linux": "language_server_linux",
    "Darwin": "language_server_macos",
    "Windows": "language_server_windows",
}

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# ─── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)


# ─── Connection manager ────────────────────────────────────────────────────────

class ConnectionManager:
    """Manages the reusable httpx client and cached Language Server connection."""

    def __init__(self):
        self._connection: dict | None = None
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(http2=True, verify=False, timeout=LS_TIMEOUT)
        return self._client

    def reset(self):
        """Close and discard the client and cached connection."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._connection = None
        log.info("ConnectionManager reset (stale connection discarded)")

    def get_connection(self) -> dict | None:
        """Return cached connection, detecting if necessary."""
        if not self._connection:
            self._connection = detect_language_server(self)
        return self._connection

    def invalidate_connection(self):
        self._connection = None


_mgr = ConnectionManager()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _ls_headers(csrf_token: str) -> dict:
    """Return the standard headers for Language Server API requests."""
    return {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "X-Codeium-Csrf-Token": csrf_token,
    }


def _quota_sort_key(x: dict):
    """Sort key: exhausted pools/models first, then by used percentage descending."""
    return (not x["is_exhausted"], -(x["used_percentage"] or 0))


def get_ip() -> str:
    """Return the machine's primary LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ─── Language Server detection ─────────────────────────────────────────────────

def detect_language_server(mgr: ConnectionManager) -> dict | None:
    """Detect the Antigravity Language Server process and extract connection params.

    Uses psutil for cross-platform process detection (Linux, macOS, Windows).
    """
    os_name = platform.system()
    ls_name = _LS_PROCESS_NAMES.get(os_name, "language_server")
    log.info("Scanning for Language Server process: %s", ls_name)

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = proc.info["name"] or ""
            cmdline = proc.info["cmdline"] or []
            cmd_str = " ".join(cmdline)

            if ls_name not in name and ls_name not in cmd_str:
                continue
            if "--extension_server_port" not in cmd_str:
                continue

            pid = proc.info["pid"]

            token_match = re.search(r"--csrf_token[=\s]+([a-zA-Z0-9\-]+)", cmd_str)
            port_match = re.search(r"--extension_server_port[=\s]+(\d+)", cmd_str)

            if not token_match:
                continue

            csrf_token = token_match.group(1)
            extension_port = int(port_match.group(1)) if port_match else 0

            ports = []
            try:
                for conn in proc.net_connections(kind="inet"):
                    if conn.status == psutil.CONN_LISTEN:
                        p = conn.laddr.port
                        if p not in ports:
                            ports.append(p)
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

            ports.sort()
            log.info("Found Language Server pid=%s, testing ports: %s", pid, ports)

            for port in ports:
                if _test_port(mgr, port, csrf_token):
                    connection = {
                        "port": port,
                        "csrf_token": csrf_token,
                        "pid": pid,
                        "extension_port": extension_port,
                    }
                    log.info("Connected to Language Server on port %s", port)
                    return connection

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    log.warning("Language Server not found")
    return None


def _test_port(mgr: ConnectionManager, port: int, csrf_token: str) -> bool:
    """Test if a port responds to the Language Server API via HTTP/2."""
    try:
        resp = mgr.client.post(
            f"https://127.0.0.1:{port}/{LS_SERVICE}/GetUnleashData",
            json={"wrapper_data": {}},
            headers=_ls_headers(csrf_token),
        )
        if resp.status_code == 200:
            resp.json()  # Validate JSON
            return True
    except Exception as e:
        log.debug("Port %s test failed: %s", port, e)
    return False


# ─── Quota fetching ────────────────────────────────────────────────────────────

def fetch_quota(mgr: ConnectionManager, connection: dict) -> dict:
    """Fetch quota data from the Language Server's GetUserStatus API via HTTP/2."""
    resp = mgr.client.post(
        f"https://127.0.0.1:{connection['port']}/{LS_SERVICE}/GetUserStatus",
        json={
            "metadata": {
                "ideName": "antigravity",
                "extensionName": "antigravity",
                "locale": "en",
            }
        },
        headers=_ls_headers(connection["csrf_token"]),
    )
    resp.raise_for_status()
    return resp.json()


# ─── Quota parsing ─────────────────────────────────────────────────────────────

def _parse_credit_block(monthly_raw, available_raw) -> dict | None:
    """Parse a single credit block (prompt or flow) into a normalised dict."""
    if not monthly_raw or available_raw is None:
        return None
    monthly, available = int(monthly_raw), int(available_raw)
    if monthly == 0:
        return None
    used = monthly - available
    return {
        "available": available,
        "monthly": monthly,
        "used": used,
        "used_percentage": round(used / monthly * 100, 1),
        "remaining_percentage": round(available / monthly * 100, 1),
    }


def parse_quota_response(data: dict) -> dict:
    """Parse the raw GetUserStatus response into a clean format."""
    user_status = data.get("userStatus", {})
    plan_status = user_status.get("planStatus", {})
    plan_info = plan_status.get("planInfo", {})

    prompt_credits = _parse_credit_block(
        plan_info.get("monthlyPromptCredits"),
        plan_status.get("availablePromptCredits"),
    )
    flow_credits = _parse_credit_block(
        plan_info.get("monthlyFlowCredits"),
        plan_status.get("availableFlowCredits"),
    )

    # Model quotas
    raw_models = (
        user_status.get("cascadeModelConfigData", {}).get("clientModelConfigs", [])
    )

    models = []
    now = datetime.now(timezone.utc)

    for m in raw_models:
        quota_info = m.get("quotaInfo")
        if not quota_info:
            continue

        remaining_fraction = quota_info.get("remainingFraction")
        reset_time_str = quota_info.get("resetTime", "")

        try:
            reset_time = datetime.fromisoformat(reset_time_str.replace("Z", "+00:00"))
            time_until_reset_ms = int((reset_time - now).total_seconds() * 1000)
        except Exception:
            time_until_reset_ms = 0

        remaining_pct = (
            round(remaining_fraction * 100, 1)
            if remaining_fraction is not None
            else None
        )
        used_pct = (
            round((1 - remaining_fraction) * 100, 1)
            if remaining_fraction is not None
            else None
        )

        models.append({
            "label": m.get("label", "Unknown"),
            "model_id": m.get("modelOrAlias", {}).get("model", "unknown"),
            "remaining_fraction": remaining_fraction,
            "remaining_percentage": remaining_pct,
            "used_percentage": used_pct,
            "is_exhausted": (remaining_fraction == 0) if remaining_fraction is not None else False,
            "reset_time_iso": reset_time_str,
            "time_until_reset_ms": time_until_reset_ms,
        })

    models.sort(key=_quota_sort_key)

    # Group models into quota pools (same reset_time + remaining_fraction = same pool)
    pool_map: dict[str, list] = {}
    for m in models:
        pool_key = f"{m['reset_time_iso']}|{m['remaining_fraction']}"
        pool_map.setdefault(pool_key, []).append(m)

    pools = []
    for pool_models in pool_map.values():
        first = pool_models[0]
        pools.append({
            "name": _derive_pool_name([m["label"] for m in pool_models]),
            "models": pool_models,
            "model_count": len(pool_models),
            "remaining_fraction": first["remaining_fraction"],
            "remaining_percentage": first["remaining_percentage"],
            "used_percentage": first["used_percentage"],
            "is_exhausted": first["is_exhausted"],
            "reset_time_iso": first["reset_time_iso"],
            "time_until_reset_ms": first["time_until_reset_ms"],
        })

    pools.sort(key=_quota_sort_key)

    return {
        "timestamp": now.isoformat(),
        "plan_name": plan_info.get("planName", "Unknown"),
        "plan_tier": plan_info.get("teamsTier", ""),
        "prompt_credits": prompt_credits,
        "flow_credits": flow_credits,
        "models": models,
        "pools": pools,
        "user_name": user_status.get("name", ""),
        "user_email": user_status.get("email", ""),
    }


def _derive_pool_name(labels: list) -> str:
    """Derive a descriptive pool name from a list of model labels."""
    if len(labels) == 1:
        return labels[0]

    families = set()
    for label in labels:
        lower = label.lower()
        if "claude" in lower:
            families.add("Claude")
        elif "gemini" in lower:
            families.add("Gemini")
        elif "gpt" in lower:
            families.add("GPT")
        else:
            families.add(label.split()[0])

    if len(families) == 1:
        return f"{list(families)[0]} Models"

    family_list = sorted(families)
    if len(family_list) <= 3:
        return " / ".join(family_list) + " Models"
    return "Premium Models"


# ─── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/quota")
def api_quota():
    connection = _mgr.get_connection()

    if not connection:
        return (
            jsonify({"error": "Language Server not found. Is Antigravity running?"}),
            503,
        )

    try:
        raw_data = fetch_quota(_mgr, connection)
        return jsonify(parse_quota_response(raw_data))
    except Exception as e:
        log.warning("Quota fetch failed (%s), resetting and retrying: %s", type(e).__name__, e)
        _mgr.reset()
        connection = _mgr.get_connection()
        if not connection:
            return jsonify({"error": f"Quota fetch failed: {e}"}), 500
        try:
            raw_data = fetch_quota(_mgr, connection)
            return jsonify(parse_quota_response(raw_data))
        except Exception as e2:
            log.error("Quota fetch failed after retry: %s", e2)
            return jsonify({"error": f"Quota fetch failed: {e2}"}), 500


# ─── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    print(f"🚀 Antigravity Quota Monitor starting on http://{get_ip()}:{APP_PORT}")
    app.run(host="0.0.0.0", port=APP_PORT, debug=False)
