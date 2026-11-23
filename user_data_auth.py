"""End-user OAuth and Conversational Analytics MCP access for BigQuery queries."""
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import requests
from cryptography.fernet import Fernet, InvalidToken
from google.cloud import firestore
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

PROJECT = "aad-analitica-oee"
AGENT = "projects/aad-analitica-oee/locations/eu/dataAgents/agent_33fe6cdf-ec2d-4cd8-93c7-a5c87fc4c345"
ADHESIVE_AGENT = "projects/aad-analitica-oee/locations/eu/dataAgents/oee-oribay"
DATA_AGENTS = {"ventas": AGENT, "adhesiva": ADHESIVE_AGENT}
MCP_URL = "https://geminidataanalytics.googleapis.com/mcp"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
SCOPES = "https://www.googleapis.com/auth/geminidataanalytics https://www.googleapis.com/auth/bigquery.readonly " + DRIVE_SCOPE + " openid email"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class UserBigQueryAuthError(RuntimeError):
    pass


def _db():
    return firestore.Client(project=PROJECT)


def _client_id():
    return os.environ.get("ORIBAY_GOOGLE_OAUTH_CLIENT_ID", "").strip()


def _client_secret():
    return os.environ.get("ORIBAY_GOOGLE_OAUTH_CLIENT_SECRET", "").strip()


def _redirect_uri(request_root):
    return os.environ.get("ORIBAY_GOOGLE_OAUTH_REDIRECT_URI", "").strip() or (
        request_root.rstrip("/") + "/auth/google/bigquery/callback")


def _fernet():
    key = os.environ.get("ORIBAY_OAUTH_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise UserBigQueryAuthError("Falta configurar el cifrado de la autorización de BigQuery.")
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise UserBigQueryAuthError("La clave de cifrado OAuth no es válida.") from exc


def begin_oauth(owner, redirect_uri):
    if not _client_id() or not _client_secret():
        raise UserBigQueryAuthError("La conexión personal de BigQuery aún no está configurada en el sitio.")
    owner = owner.lower().strip()
    state = secrets.token_urlsafe(32)
    _db().collection("oribay_bq_oauth_states").document(state).set({
        "owner": owner,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    })
    params = {
        "client_id": _client_id(), "redirect_uri": redirect_uri,
        "response_type": "code", "scope": SCOPES, "access_type": "offline",
        "prompt": "consent", "include_granted_scopes": "true", "state": state,
        "login_hint": owner,
    }
    from urllib.parse import urlencode
    return AUTH_URL + "?" + urlencode(params)


def finish_oauth(owner, state, code, redirect_uri):
    state_ref = _db().collection("oribay_bq_oauth_states").document(state)
    state_snapshot = state_ref.get()
    if not state_snapshot.exists:
        raise UserBigQueryAuthError("La autorización ha caducado. Vuelve a conectar BigQuery.")
    state_data = state_snapshot.to_dict()
    state_ref.delete()
    expiry = datetime.fromisoformat(state_data["expires_at"])
    if expiry < datetime.now(timezone.utc) or state_data.get("owner") != owner.lower():
        raise UserBigQueryAuthError("La identidad de la autorización no coincide con la sesión del sitio.")

    response = requests.post(TOKEN_URL, data={
        "code": code, "client_id": _client_id(), "client_secret": _client_secret(),
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=25)
    if not response.ok:
        raise UserBigQueryAuthError("Google no pudo completar la autorización de BigQuery.")
    tokens = response.json()
    identity_token = tokens.get("id_token")
    if not identity_token:
        raise UserBigQueryAuthError("Google no devolvió una identidad verificable para esta autorización.")
    claims = id_token.verify_oauth2_token(identity_token, google_requests.Request(), _client_id())
    if claims.get("email", "").lower() != owner.lower() or not claims.get("email_verified"):
        raise UserBigQueryAuthError("Autoriza BigQuery con la misma cuenta que usas para entrar al sitio.")
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        existing = _db().collection("oribay_bq_oauth").document(owner.lower()).get()
        if existing.exists:
            refresh_token = _fernet().decrypt(existing.to_dict()["refresh_token_enc"].encode()).decode()
    if not refresh_token:
        raise UserBigQueryAuthError("Google no entregó autorización renovable; vuelve a conectar y acepta el acceso solicitado.")
    encrypted = _fernet().encrypt(refresh_token.encode()).decode()
    _db().collection("oribay_bq_oauth").document(owner.lower()).set({
        "owner": owner.lower(), "refresh_token_enc": encrypted,
        "scopes": tokens.get("scope", ""), "connected_at": datetime.now(timezone.utc).isoformat(),
        "google_subject": claims.get("sub", ""),
    })


def disconnect_oauth(owner):
    _db().collection("oribay_bq_oauth").document(owner.lower()).delete()


def is_connected(owner):
    return _db().collection("oribay_bq_oauth").document(owner.lower()).get().exists


def drive_is_connected(owner):
    snapshot = _db().collection("oribay_bq_oauth").document(owner.lower()).get()
    return snapshot.exists and DRIVE_SCOPE in snapshot.to_dict().get("scopes", "").split()


def user_access_token(owner):
    owner = owner.lower().strip()
    snapshot = _db().collection("oribay_bq_oauth").document(owner).get()
    if not snapshot.exists:
        raise UserBigQueryAuthError("Conecta tu cuenta de Google para consultar con tus permisos personales de BigQuery.")
    try:
        refresh_token = _fernet().decrypt(snapshot.to_dict()["refresh_token_enc"].encode()).decode()
    except (InvalidToken, KeyError, ValueError) as exc:
        raise UserBigQueryAuthError("No se pudo recuperar la autorización guardada. Vuelve a conectar BigQuery.") from exc
    response = requests.post(TOKEN_URL, data={
        "client_id": _client_id(), "client_secret": _client_secret(),
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }, timeout=25)
    if not response.ok:
        raise UserBigQueryAuthError("La autorización de BigQuery ha caducado o fue revocada. Vuelve a conectar tu cuenta.")
    return response.json()["access_token"]


def _decode_mcp_response(response):
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        return response.json()
    data = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw and raw != "[DONE]":
                try:
                    data.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    return data[-1] if data else {}


def _mcp_call(session, headers, message_id, method, params=None):
    response = session.post(MCP_URL, headers=headers, json={
        "jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {},
    }, timeout=(15, 120))
    if response.status_code >= 400:
        raise UserBigQueryAuthError("Conversational Analytics rechazó la consulta con la autorización personal. Comprueba tus permisos sobre el agente y BigQuery.")
    value = _decode_mcp_response(response)
    if isinstance(value, list):
        value = next((item for item in reversed(value) if isinstance(item, dict) and ("result" in item or "error" in item)), {})
    if value.get("error"):
        message = value["error"].get("message", "Error MCP")
        raise UserBigQueryAuthError(f"No se pudo consultar Ventas Oribay con tu cuenta: {message[:300]}")
    result = value.get("result", value)
    if isinstance(result, dict) and result.get("isError"):
        details = " ".join(part.get("text", "") for part in result.get("content", []) if isinstance(part, dict))
        raise UserBigQueryAuthError(f"No se pudo consultar Ventas Oribay con tu cuenta: {details[:300] or 'error MCP'}")
    return result, response.headers


def _chart_from_rows(rows, question):
    if len(rows) < 2 or not re.search(r"gr[aá]fic|chart|barras?|l[ií]nea", question, re.IGNORECASE):
        return None
    columns = list(rows[0])
    category = next((name for name in columns if re.search(r"mes|month|fecha|period", name, re.IGNORECASE)), None)
    metric = next((name for name in columns if re.search(r"factur|importe|eur|revenue|sales", name, re.IGNORECASE)), None)
    if not category or not metric or category == metric:
        return None

    def number(value):
        if isinstance(value, (int, float)):
            return float(value)
        clean = re.sub(r"[^\d,.-]", "", str(value or ""))
        if "," in clean and "." in clean:
            clean = clean.replace(".", "").replace(",", ".") if clean.rfind(",") > clean.rfind(".") else clean.replace(",", "")
        elif "," in clean:
            clean = clean.replace(",", ".") if len(clean.rsplit(",", 1)[-1]) <= 2 else clean.replace(",", "")
        try:
            return float(clean)
        except ValueError:
            return None

    values = [{category: str(row.get(category, "")), metric: number(row.get(metric))} for row in rows[:50] if isinstance(row, dict)]
    values = [row for row in values if row[category] and row[metric] is not None]
    if len(values) < 2:
        return None
    return {"$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": "Facturación de piezas", "width": 620, "height": 300,
            "data": {"values": values}, "mark": "bar",
            "encoding": {"x": {"field": category, "type": "nominal", "title": "Periodo", "sort": None},
                         "y": {"field": metric, "type": "quantitative", "title": "Facturación (€)"},
                         "tooltip": [{"field": category, "type": "nominal"}, {"field": metric, "type": "quantitative", "format": ",.2f"}]}}


def _extract_result(value, question=""):
    # The MCP chat tool returns Conversational Analytics Message objects. Only
    # FINAL_RESPONSE is user-facing; PROGRESS, THOUGHT and data jobs are not.
    messages = []
    for part in value.get("content", []) if isinstance(value, dict) else []:
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            continue
        try:
            parsed = json.loads(part["text"])
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("systemMessage"), dict):
            messages.append(parsed["systemMessage"])
    if messages:
        answers, tables, sql, charts, chart_rows = [], [], [], [], []
        for message in messages:
            typed_text = message.get("text") or {}
            if typed_text.get("textType") == "FINAL_RESPONSE":
                answers.extend(part for part in typed_text.get("parts", []) if isinstance(part, str) and part.strip())
            data_message = message.get("data") or {}
            if isinstance(data_message.get("generatedSql"), str):
                sql.append(data_message["generatedSql"])
            data_result = data_message.get("result") or {}
            rows = data_result.get("data") or []
            if rows and isinstance(rows[0], dict):
                chart_rows = rows
                columns = list(dict.fromkeys(key for row in rows[:200] if isinstance(row, dict) for key in row))[:20]
                def cell(value):
                    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")
                table = "| " + " | ".join(columns) + " |\n| " + " | ".join("---" for _ in columns) + " |"
                table += "".join("\n| " + " | ".join(cell(row.get(column)) for column in columns) + " |" for row in rows[:200] if isinstance(row, dict))
                tables.append(table)
            chart_result = (message.get("chart") or {}).get("result") or {}
            spec = chart_result.get("vegaConfig")
            if isinstance(spec, dict) and spec:
                charts.append(spec)
        if not charts:
            generated_chart = _chart_from_rows(chart_rows, question)
            if generated_chart:
                charts.append(generated_chart)
        if not answers:
            raise UserBigQueryAuthError("Ventas Oribay no devolvió una respuesta final; inténtalo de nuevo.")
        return {"answer": "\n\n".join(answers), "tables": tables[:8], "sql": sql[:8], "charts": charts[:8]}

    answer, final_answers, tables, sql, charts = [], [], [], [], []
    seen = set()

    def walk(node, key=""):
        if isinstance(node, dict):
            if node.get("mark") and node.get("data"):
                encoded = json.dumps(node, sort_keys=True, ensure_ascii=False)
                if encoded not in seen:
                    charts.append(node); seen.add(encoded)
            name = str(node.get("name", node.get("title", key))).lower()
            for child_key, child in node.items():
                child_name = str(child_key).lower()
                if child_name in ("name", "title"):
                    continue
                if isinstance(child, str) and child.strip():
                    if "sql" in child_name or name in ("generated sql", "sql"):
                        sql.append(child)
                    elif "table" in child_name or name in ("data result", "table"):
                        tables.append(child)
                    elif child_name in ("text", "answer", "response", "content"):
                        try:
                            parsed = json.loads(child)
                        except (ValueError, TypeError):
                            parsed = None
                        if parsed is not None:
                            walk(parsed, child_name)
                        elif name == "final response":
                            if child not in final_answers:
                                final_answers.append(child)
                        elif child not in answer:
                            answer.append(child)
                else:
                    walk(child, name if child_name == "parts" and name else child_name)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and node.strip():
            if "sql" in key:
                sql.append(node)
            elif "table" in key or "data" in key:
                tables.append(node)
            elif node not in answer:
                answer.append(node)

    walk(value)
    return {"answer": "\n\n".join(final_answers or answer), "tables": tables[:8], "sql": sql[:8], "charts": charts[:8]}


def list_data_agents_as_user(owner):
    """Discover agents visible to the connected user; the data API still enforces access."""
    token = user_access_token(owner)
    with requests.Session() as session:
        headers = {
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }
        initialized, response_headers = _mcp_call(session, headers, 1, "initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "oribay-intelligence", "version": "1.0.0"},
        })
        if response_headers.get("Mcp-Session-Id"):
            headers["Mcp-Session-Id"] = response_headers["Mcp-Session-Id"]
        headers["MCP-Protocol-Version"] = initialized.get("protocolVersion", "2025-03-26")
        if headers.get("Mcp-Session-Id"):
            session.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=15)
        listed, _ = _mcp_call(session, headers, 2, "tools/list")
        tool = next((item for item in listed.get("tools", []) if item.get("name") == "list_data_agents"), None)
        if not tool:
            raise UserBigQueryAuthError("No se pudo comprobar qué agentes tienes disponibles.")
        schema = (tool.get("inputSchema") or {}).get("properties", {})
        args = {"parent": "projects/aad-analitica-oee/locations/eu"}
        args = {key: value for key, value in args.items() if key in schema}
        result, _ = _mcp_call(session, headers, 3, "tools/call", {"name": "list_data_agents", "arguments": args})
        raw = json.dumps(result, ensure_ascii=False)
        for item in result.get("content", []):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                raw += " " + item["text"]
        return [name for name, resource in DATA_AGENTS.items() if resource in raw]


def query_data_agent_as_user(owner, question, chart_request="", history=None, agent="ventas"):
    if agent not in DATA_AGENTS:
        raise ValueError("Agente de datos no permitido")
    token = user_access_token(owner)
    session = requests.Session()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26",
    }
    result, response_headers = _mcp_call(session, headers, 1, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "oribay-intelligence", "version": "1.0.0"},
    })
    session_id = response_headers.get("Mcp-Session-Id")
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    headers["MCP-Protocol-Version"] = result.get("protocolVersion", "2025-03-26")
    if session_id:
        session.post(MCP_URL, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=15)
    result, _ = _mcp_call(session, headers, 2, "tools/list")
    tools = result.get("tools", [])
    chat_tool = next((tool for tool in tools if tool.get("name") == "chat"), None)
    if not chat_tool:
        raise UserBigQueryAuthError("La conexión de Google no ofrece la herramienta de consulta de agentes.")
    messages = []
    for item in (history or [])[-10:]:
        if isinstance(item, dict) and ("userMessage" in item or "systemMessage" in item):
            messages.append(item)
    messages.append({"userMessage": {"text": question}})
    args = {"parent": "projects/aad-analitica-oee/locations/eu",
            "dataAgentContext": {"dataAgent": DATA_AGENTS[agent]},
            "messages": messages}
    schema = (chat_tool.get("inputSchema") or {}).get("properties", {})
    args = {key: val for key, val in args.items() if key in schema}
    result, _ = _mcp_call(session, headers, 3, "tools/call", {"name": "chat", "arguments": args})
    session.close()
    return _extract_result(result, question + " " + chart_request)
