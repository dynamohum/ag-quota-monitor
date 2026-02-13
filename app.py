"""
Antigravity Quota Monitor — Flask backend
Detects the Antigravity Language Server process, proxies the GetUserStatus API
over HTTP/2, and serves a web dashboard showing model quota usage.
"""

import json
import re
import subprocess
from datetime import datetime, timezone

import httpx
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Cached connection info
_cached_connection = None
# Reusable httpx client with HTTP/2
_http_client = None


def _get_http_client():
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(http2=True, verify=False, timeout=30.0)
    return _http_client


def _run_cmd(cmd: str) -> str:
    """Run a shell command and return stdout."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip()
    except Exception:
        return ""


def detect_language_server():
    """Detect the Antigravity Language Server process and extract connection params."""
    global _cached_connection

    stdout = _run_cmd("pgrep -af language_server_linux")
    if not stdout:
        return None

    lines = stdout.split("\n")
    for line in lines:
        if "--extension_server_port" not in line:
            continue

        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue

        pid = int(parts[0])
        cmd = parts[1]

        token_match = re.search(r"--csrf_token[=\s]+([a-zA-Z0-9\-]+)", cmd)
        port_match = re.search(r"--extension_server_port[=\s]+(\d+)", cmd)

        if not token_match:
            continue

        csrf_token = token_match.group(1)
        extension_port = int(port_match.group(1)) if port_match else 0

        # Get listening ports
        ss_output = _run_cmd(f'ss -tlnp 2>/dev/null | grep "pid={pid},"')
        ports = []

        if ss_output:
            for ss_line in ss_output.split("\n"):
                port_m = re.search(r"(?:\*|[\d.]+|\[[\da-f:]*\]):(\d+)", ss_line)
                if port_m:
                    p = int(port_m.group(1))
                    if p not in ports:
                        ports.append(p)

        if not ports:
            lsof_output = _run_cmd(
                f"lsof -nP -a -iTCP -sTCP:LISTEN -p {pid} 2>/dev/null"
            )
            if lsof_output:
                for lsof_line in lsof_output.split("\n"):
                    port_m = re.search(
                        r"(?:\*|[\d.]+|\[[\da-f:]+\]):(\d+)\s+\(LISTEN\)", lsof_line
                    )
                    if port_m:
                        p = int(port_m.group(1))
                        if p not in ports:
                            ports.append(p)

        ports.sort()

        # Test each port via HTTP/2
        for port in ports:
            if _test_port(port, csrf_token):
                _cached_connection = {
                    "port": port,
                    "csrf_token": csrf_token,
                    "pid": pid,
                    "extension_port": extension_port,
                }
                return _cached_connection

    return None


def _test_port(port: int, csrf_token: str) -> bool:
    """Test if a port responds to the Language Server API via HTTP/2."""
    try:
        client = _get_http_client()
        resp = client.post(
            f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUnleashData",
            json={"wrapper_data": {}},
            headers={
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "X-Codeium-Csrf-Token": csrf_token,
            },
        )
        if resp.status_code == 200:
            resp.json()  # Validate JSON
            return True
    except Exception:
        pass
    return False


def fetch_quota(connection: dict) -> dict:
    """Fetch quota data from the Language Server's GetUserStatus API via HTTP/2."""
    client = _get_http_client()
    resp = client.post(
        f"https://127.0.0.1:{connection['port']}/exa.language_server_pb.LanguageServerService/GetUserStatus",
        json={
            "metadata": {
                "ideName": "antigravity",
                "extensionName": "antigravity",
                "locale": "en",
            }
        },
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "X-Codeium-Csrf-Token": connection["csrf_token"],
        },
    )
    resp.raise_for_status()
    return resp.json()


def parse_quota_response(data: dict) -> dict:
    """Parse the raw GetUserStatus response into a clean format."""
    user_status = data.get("userStatus", {})
    plan_status = user_status.get("planStatus", {})
    plan_info = plan_status.get("planInfo", {})

    # Prompt credits
    prompt_credits = None
    monthly = plan_info.get("monthlyPromptCredits")
    available = plan_status.get("availablePromptCredits")
    if monthly and available is not None:
        monthly = int(monthly)
        available = int(available)
        if monthly > 0:
            prompt_credits = {
                "available": available,
                "monthly": monthly,
                "used": monthly - available,
                "used_percentage": round(((monthly - available) / monthly) * 100, 1),
                "remaining_percentage": round((available / monthly) * 100, 1),
            }

    # Flow credits
    flow_credits = None
    monthly_flow = plan_info.get("monthlyFlowCredits")
    available_flow = plan_status.get("availableFlowCredits")
    if monthly_flow and available_flow is not None:
        monthly_flow = int(monthly_flow)
        available_flow = int(available_flow)
        if monthly_flow > 0:
            flow_credits = {
                "available": available_flow,
                "monthly": monthly_flow,
                "used": monthly_flow - available_flow,
                "used_percentage": round(
                    ((monthly_flow - available_flow) / monthly_flow) * 100, 1
                ),
                "remaining_percentage": round(
                    (available_flow / monthly_flow) * 100, 1
                ),
            }

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
            reset_time = datetime.fromisoformat(
                reset_time_str.replace("Z", "+00:00")
            )
            time_until_reset_ms = int(
                (reset_time - now).total_seconds() * 1000
            )
        except Exception:
            reset_time = None
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

        model_entry = {
            "label": m.get("label", "Unknown"),
            "model_id": m.get("modelOrAlias", {}).get("model", "unknown"),
            "remaining_fraction": remaining_fraction,
            "remaining_percentage": remaining_pct,
            "used_percentage": used_pct,
            "is_exhausted": remaining_fraction == 0
            if remaining_fraction is not None
            else False,
            "reset_time_iso": reset_time_str,
            "time_until_reset_ms": time_until_reset_ms,
        }
        models.append(model_entry)

    # Sort: exhausted first, then by used_percentage descending
    models.sort(
        key=lambda x: (
            not x["is_exhausted"],
            -(x["used_percentage"] or 0),
        )
    )

    # Group models into quota pools (same reset_time + remaining_fraction = same pool)
    pool_map = {}
    for m in models:
        pool_key = f"{m['reset_time_iso']}|{m['remaining_fraction']}"
        if pool_key not in pool_map:
            pool_map[pool_key] = []
        pool_map[pool_key].append(m)

    pools = []
    for pool_key, pool_models in pool_map.items():
        # Derive a pool name from the model labels
        labels = [m["label"] for m in pool_models]
        pool_name = _derive_pool_name(labels)

        first = pool_models[0]
        pools.append({
            "name": pool_name,
            "models": pool_models,
            "model_count": len(pool_models),
            "remaining_fraction": first["remaining_fraction"],
            "remaining_percentage": first["remaining_percentage"],
            "used_percentage": first["used_percentage"],
            "is_exhausted": first["is_exhausted"],
            "reset_time_iso": first["reset_time_iso"],
            "time_until_reset_ms": first["time_until_reset_ms"],
        })

    # Sort pools: exhausted first, then by used_percentage descending
    pools.sort(
        key=lambda x: (
            not x["is_exhausted"],
            -(x["used_percentage"] or 0),
        )
    )

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

    # Check for common prefixes/families
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

    # Mixed pool — call it "Premium Models" or list families
    family_list = sorted(families)
    if len(family_list) <= 3:
        return " / ".join(family_list) + " Models"
    return "Premium Models"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/quota")
def api_quota():
    global _cached_connection, _http_client

    # Try cached connection first
    connection = _cached_connection
    if not connection:
        connection = detect_language_server()

    if not connection:
        return (
            jsonify(
                {"error": "Language Server not found. Is Antigravity running?"}
            ),
            503,
        )

    try:
        raw_data = fetch_quota(connection)
        parsed = parse_quota_response(raw_data)
        return jsonify(parsed)
    except Exception as e:
        # Connection may have gone stale, try re-detecting
        _cached_connection = None
        # Reset the HTTP client too in case of stale H2 connection
        if _http_client:
            try:
                _http_client.close()
            except Exception:
                pass
            _http_client = None
        connection = detect_language_server()
        if connection:
            try:
                raw_data = fetch_quota(connection)
                parsed = parse_quota_response(raw_data)
                return jsonify(parsed)
            except Exception as e2:
                return jsonify({"error": f"Quota fetch failed: {str(e2)}"}), 500
        return jsonify({"error": f"Quota fetch failed: {str(e)}"}), 500


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    print("🚀 Antigravity Quota Monitor starting on http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
