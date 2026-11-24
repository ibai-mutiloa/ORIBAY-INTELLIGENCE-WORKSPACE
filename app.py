"""Piloto paralelo para visualizar la respuesta completa de Ventas Oribay."""
import os
import uuid
import io
import json
import sqlite3
import re
import math
import hashlib
import html
from datetime import datetime, timezone, timedelta

import google.auth
from google.oauth2.credentials import Credentials
from google.auth import impersonated_credentials
from google.auth.transport.requests import AuthorizedSession
from flask import Flask, jsonify, render_template, request, send_file, g, redirect
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google.cloud import firestore
from google.cloud import storage
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from email.message import EmailMessage
from googleapiclient.discovery import build
from router import decide, draft_business_email, draft_pdf_analysis, answer_from_drive, summarize_conversation, FOLLOWUP_TERMS
from user_data_auth import (UserBigQueryAuthError, begin_oauth, disconnect_oauth,
                            finish_oauth, is_connected, query_data_agent_as_user, list_data_agents_as_user, _redirect_uri)
from user_data_auth import drive_is_connected
import drive_tools
from presentations import create_presentation

app = Flask(__name__)
PROJECT = "aad-analitica-oee"
AGENT = "projects/aad-analitica-oee/locations/eu/dataAgents/agent_33fe6cdf-ec2d-4cd8-93c7-a5c87fc4c345"
ENDPOINT = f"https://geminidataanalytics.eu.rep.googleapis.com/v1/a2a/{AGENT}/v1/message:stream"
DB_PATH = os.environ.get("ORIBAY_ALERT_DB", os.path.join(os.path.dirname(__file__), "alerts.sqlite3"))
IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE")
LIBRARY_BUCKET = os.environ.get("ORIBAY_LIBRARY_BUCKET", "aad-analitica-oee-oribay-library")
GMAIL_SENDER_SERVICE_ACCOUNT = os.environ.get("ORIBAY_GMAIL_SENDER_SERVICE_ACCOUNT", "").strip()


def gmail_credentials(sender):
    """Impersonate an authorized Workspace sender through the dedicated DWD service account."""
    if os.environ.get("ORIBAY_GMAIL_DWD_ENABLED") != "true" or not GMAIL_SENDER_SERVICE_ACCOUNT:
        raise RuntimeError("El envío desde Gmail no está configurado.")
    source_credentials = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])[0]
    credentials = impersonated_credentials.Credentials(
        source_credentials=source_credentials,
        target_principal=GMAIL_SENDER_SERVICE_ACCOUNT,
        target_scopes=["https://www.googleapis.com/auth/gmail.send"],
        subject=sender,
        lifetime=600,
    )
    credentials.refresh(google_requests.Request())
    return credentials


def current_owner():
    return (getattr(g, "user_email", None) or os.environ.get("ORIBAY_USER_EMAIL", "local-user")).lower()


def save_library_item(kind, title, payload, object_path=None):
    item_id = str(uuid.uuid4())
    item = {"owner": current_owner(), "type": kind, "title": str(title)[:180],
            "payload": payload, "object_path": object_path or "",
            "created_at": datetime.now(timezone.utc).isoformat(), "deleted": False}
    firestore.Client(project=PROJECT).collection("oribay_library").document(item_id).set(item)
    return item_id


def owned_conversation(conversation_id):
    ref = firestore.Client(project=PROJECT).collection("oribay_conversations").document(conversation_id)
    snapshot = ref.get()
    if not snapshot.exists or snapshot.to_dict().get("owner") != current_owner() or snapshot.to_dict().get("deleted"):
        return None, None
    return ref, snapshot.to_dict()


def restore_saved_analyses_as_chat_history():
    """One-time recovery of pre-chat transcripts from this user's saved analyses."""
    client = firestore.Client(project=PROJECT)
    owner = current_owner()
    marker_id = hashlib.sha256(owner.encode("utf-8")).hexdigest()
    marker = client.collection("oribay_conversation_migrations").document(marker_id)
    if marker.get().exists:
        return

    conversations = list(client.collection("oribay_conversations").where("owner", "==", owner).stream())
    cutoffs = []
    for snapshot in conversations:
        try:
            cutoffs.append(datetime.fromisoformat(snapshot.to_dict().get("created_at", "").replace("Z", "+00:00")))
        except (TypeError, ValueError):
            pass
    cutoff = min(cutoffs) if cutoffs else None

    library = []
    for snapshot in client.collection("oribay_library").where("owner", "==", owner).stream():
        item = snapshot.to_dict()
        try:
            created = datetime.fromisoformat(item.get("created_at", "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if cutoff and created >= cutoff:
            continue
        item["_id"] = snapshot.id
        item["_created"] = created
        library.append(item)

    analyses = [item for item in library if item.get("type") == "analysis"
                and (item.get("payload") or {}).get("question")
                and (item.get("payload") or {}).get("answer")]
    analyses.sort(key=lambda item: item["_created"])
    restored = []
    if analyses:
        conversation_id = str(uuid.uuid4())
        ref = client.collection("oribay_conversations").document(conversation_id)
        first, last = analyses[0]["_created"], analyses[-1]["_created"]
        ref.set({"owner": owner, "title": "Historial anterior · análisis recuperados",
                 "preview": "Conversaciones reconstruidas a partir de los análisis guardados.",
                 "created_at": first.isoformat(), "updated_at": datetime.now(timezone.utc).isoformat(),
                 "message_count": len(analyses) * 2, "deleted": False, "source": "library-history-recovery"})

        emails = [item for item in library if item.get("type") == "email"]
        documents = [item for item in library if item.get("type") == "document" and not item.get("deleted")]
        for item in analyses:
            payload = item.get("payload") or {}
            question = str(payload.get("question", ""))[:2000]
            answer = str(payload.get("answer", ""))[:12000]
            created = item["_created"]
            ref.collection("messages").add({"kind": "user", "question": question,
                                             "created_at": created.isoformat(), "recovered": True})

            charts = payload.get("charts", []) if isinstance(payload.get("charts"), list) else []
            tables = payload.get("tables", []) if isinstance(payload.get("tables"), list) else []
            if len(json.dumps({"charts": charts, "tables": tables}, ensure_ascii=False).encode("utf-8")) > 650_000:
                charts, tables = [], []
            assistant = {"kind": "assistant", "answer": answer, "route": payload.get("route", "ventas"),
                         "data_agent": payload.get("data_agent", "ventas"),
                         "title": item.get("title", ""), "charts": charts, "tables": tables,
                         "created_at": (created + timedelta(microseconds=1)).isoformat(), "recovered": True}
            if not item.get("deleted"):
                assistant["library_id"] = item["_id"]
            if assistant["route"] == "correo":
                matching = [entry for entry in emails if entry.get("title") == ((entry.get("payload") or {}).get("subject") or "")]
                if matching:
                    email_item = min(matching, key=lambda entry: abs((entry["_created"] - created).total_seconds()))
                    ep = email_item.get("payload") or {}
                    assistant["email_snapshot"] = {key: ep.get(key, "") for key in ("to", "subject", "body")}
                    if not email_item.get("deleted"):
                        assistant["email_library_id"] = email_item["_id"]
            if assistant["route"] == "pdf":
                matching_docs = [entry for entry in documents if abs((entry["_created"] - created).total_seconds()) <= 600]
                if matching_docs:
                    assistant["document_library_id"] = min(matching_docs, key=lambda entry: abs((entry["_created"] - created).total_seconds()))["_id"]
            ref.collection("messages").add(assistant)
            restored.append({"question": question, "answer": answer, "created": created})

    marker.set({"owner": owner, "migrated_at": datetime.now(timezone.utc).isoformat(),
                "restored_count": len(restored)})


def conversation_message(conversation_ref, kind, **values):
    conversation_ref.collection("messages").add({
        "kind": kind, "created_at": datetime.now(timezone.utc).isoformat(), **values
    })


def save_conversation_result(conversation_ref, question, result):
    answer = str(result.get("answer", ""))[:12000]
    plan = result.get("plan") or {}
    source_question = str(plan.get("followup_source") or question)[:1200]
    context = {"agent": str(result.get("data_agent", "ventas")),
               "source_question": source_question,
               "references": list(dict.fromkeys(re.findall(r"\b[A-Z]{1,5}[A-Z0-9.-]*\d[A-Z0-9.-]*\b", source_question, re.IGNORECASE)))[:20],
               "years": list(dict.fromkeys(re.findall(r"\b20\d{2}\b", source_question)))[:8],
               "top_n": int(re.search(r"\btop\s*(\d{1,2})\b", source_question, re.IGNORECASE).group(1))
               if re.search(r"\btop\s*(\d{1,2})\b", source_question, re.IGNORECASE) else None}
    conversation_message(conversation_ref, "assistant", answer=answer,
                         route=str(result.get("route", "ventas")),
                         data_agent=str(result.get("data_agent", "ventas")),
                         title=str(plan.get("title", ""))[:180],
                         library_id=str(result.get("library_id", "")),
                         email_library_id=str((result.get("email") or {}).get("library_id", "")),
                         presentation=result.get("presentation") or {},
                         context=context,
                         plan={key: plan.get(key) for key in (
                             "alert_metric", "alert_operator", "alert_threshold", "alert_period",
                             "alert_frequency", "alert_filters", "needs_clarification", "clarification",
                             "context_used", "context_reference", "drive_action", "drive_query",
                             "drive_file", "drive_destination", "drive_path") if key in plan})
    conversation_ref.update({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "preview": answer[:180],
        "message_count": firestore.Increment(2),
    })


def conversation_recent_agent_turns(conversation_ref, max_turns=5):
    """Only the latest uninterrupted stretch of one data agent is remembered."""
    if conversation_ref is None:
        return []
    messages = list(conversation_ref.collection("messages")
                    .order_by("created_at", direction=firestore.Query.DESCENDING)
                    .limit(40).stream())
    turns = []
    pending_question = ""
    for snapshot in reversed(messages):
        item = snapshot.to_dict() or {}
        if item.get("kind") == "user":
            pending_question = str(item.get("question", "")).strip()[:1200]
        elif item.get("kind") == "assistant":
            route = item.get("route")
            answer = str(item.get("answer", "")).strip()[:2400]
            message_agent = item.get("data_agent") or ("adhesiva" if route == "adhesiva" else "ventas")
            if pending_question and route in ("ventas", "adhesiva", "pdf", "correo", "presentacion") and answer:
                turns.append({"route": message_agent, "question": pending_question, "answer": answer,
                              "context": item.get("context") or {}})
            elif pending_question:
                turns.append({"route": None})
            pending_question = ""
    if not turns or turns[-1].get("route") not in ("ventas", "adhesiva"):
        return []
    recent_agent = turns[-1]["route"]
    segment = []
    for turn in reversed(turns):
        if turn.get("route") != recent_agent:
            break
        segment.append(turn)
        if len(segment) >= max_turns:
            break
    return list(reversed(segment))


def needs_previous_turn(question):
    """Only references to earlier work should carry conversational data forward."""
    if FOLLOWUP_TERMS.search(question):
        return True
    if re.fullmatch(r"\s*(?:haz|crea|genera|muestra)\s+(?:un\s+)?gr[aá]fic[oa](?:\s+visual)?\s*", question, re.IGNORECASE):
        return True
    return bool(re.search(
        r"\b(?:[eé]st[aeos]|es[aeos]|aqu[eé]ll[ao]s?|anteriores?|[uú]ltim[ao]s?\s+consultas?|"
        r"lo\s+anterior|mismo\s+(?:cliente|periodo|an[aá]lisis)|"
        r"ahora|siguiendo|contin[uú]a|desglosa|ampl[ií]a|sobre\s+eso|"
        r"vimos\s+antes|estos\s+datos|esas\s+cifras|ese\s+top|"
        r"(?:haz|crea|genera)\s+(?:un\s+)?gr[aá]fico(?:\s+visual)?)\b",
        question, re.IGNORECASE))


def followup_source(question, turns):
    """Resolve a deictic follow-up to the last concrete request in the same agent segment."""
    if not needs_previous_turn(question):
        return None
    for turn in reversed(turns):
        source_question = str((turn.get("context") or {}).get("source_question") or "").strip()
        if source_question and not needs_previous_turn(source_question):
            return {**turn, "question": source_question}
        prior = str(turn.get("question", "")).strip()
        if prior and not needs_previous_turn(prior) and not re.fullmatch(
                r"(?:haz|crea|genera|muestra)\s+(?:un\s+)?(?:gr[aá]fico|presentaci[oó]n|pdf)(?:\s+visual)?",
                prior, re.IGNORECASE):
            return turn
    return None


def exceeds_requested_top(source_question, tables):
    match = re.search(r"\btop\s*(\d{1,2})\b", source_question, re.IGNORECASE)
    if not match:
        return False
    limit = int(match.group(1))
    for table in tables or []:
        lines = [line for line in str(table).splitlines() if line.strip().startswith("|")]
        data_rows = [line for line in lines[1:] if not re.fullmatch(r"[|:\-\s]+", line)]
        if data_rows and len(data_rows) > limit:
            return True
    return False


def focused_prior_answer(question, answer):
    """Select the relevant numbered section of a mixed previous answer."""
    sections = re.split(r"(?m)(?=^\s*\d+\.\s+[^\n]+\n)", answer)
    if len(sections) < 3:
        return answer
    terms = set(re.findall(r"[\w-]{5,}", question.casefold())) - {
        "genera", "generar", "informe", "piezas", "última", "consulta", "ventas", "sobre"}
    ranked = []
    for section in sections:
        heading = section.splitlines()[0].casefold() if section.strip() else ""
        score = sum(term in heading for term in terms)
        ranked.append((score, section))
    best_score, best_section = max(ranked, key=lambda item: item[0])
    return best_section.strip() if best_score else answer


def missing_report_evidence(result):
    """Do not turn an agent's request for missing identifiers into a downloadable PDF."""
    if result.get("tables") or result.get("charts"):
        return False
    answer = str(result.get("answer", ""))
    return bool(re.search(
        r"no\s+se\s+dispone\s+de\s+(?:la\s+)?informaci[oó]n\s+necesaria|"
        r"no\s+se\s+(?:ha\s+)?proporcionad[oa]\s+(?:un|la|el)|"
        r"no\s+es\s+posible\s+proceder|"
        r"por\s+favor,?\s+proporcion[ae]",
        answer, re.IGNORECASE))


def reconcile_top_two_share(answer, tables):
    """Correct a two-client concentration claim only when all row amounts are available."""
    claim = re.search(r"m[aá]s\s+del\s+\*\*(\d+(?:[.,]\d+)?)\s*%\*\*", str(answer), re.IGNORECASE)
    if not claim or not re.search(r"dos\s+principales\s+clientes", answer, re.IGNORECASE):
        return answer
    for raw_table in tables or []:
        rows = []
        for line in str(raw_table).splitlines():
            if "|" not in line:
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and not all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in cells):
                rows.append(cells)
        if len(rows) < 4:
            continue
        money_index = next((index for index, header in enumerate(rows[0])
                            if re.search(r"factur|importe|eur|€", header, re.IGNORECASE)), None)
        if money_index is None:
            continue
        amounts = []
        for row in rows[1:]:
            if money_index >= len(row):
                amounts = []
                break
            raw = re.sub(r"[^\d,.-]", "", row[money_index])
            if not raw:
                amounts = []
                break
            if "," in raw and "." in raw:
                raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
            elif "," in raw:
                raw = raw.replace(",", ".")
            try:
                amounts.append(float(raw))
            except ValueError:
                amounts = []
                break
        if len(amounts) < 3 or sum(amounts) <= 0:
            continue
        actual = 100 * sum(sorted(amounts, reverse=True)[:2]) / sum(amounts)
        claimed_floor = float(claim.group(1).replace(",", "."))
        if actual <= claimed_floor:
            corrected = f"el **{actual:.2f}".replace(".", ",") + " %**"
            return answer[:claim.start()] + corrected + answer[claim.end():]
        return answer
    return answer


def conversation_sales_context(conversation_ref, agent="ventas", max_turns=1, question=""):
    """Pass only current-agent turns to the Conversational Analytics agent."""
    turns = conversation_recent_agent_turns(conversation_ref, max_turns=max_turns)
    if not turns or turns[-1]["route"] != agent:
        return []
    transcript = []
    for turn in turns:
        transcript.extend([
            {"userMessage": {"text": turn["question"]}},
            {"systemMessage": {"text": {"parts": [focused_prior_answer(question, turn["answer"])]}}},
        ])
    return transcript


@app.before_request
def verify_user():
    if not IAP_AUDIENCE:
        return None
    assertion = request.headers.get("X-Goog-IAP-JWT-Assertion", "")
    if not assertion:
        return jsonify(error="Se requiere acceso mediante IAP."), 401
    try:
        claims = id_token.verify_token(
            assertion, google_requests.Request(), audience=IAP_AUDIENCE,
            certs_url="https://www.gstatic.com/iap/verify/public_key")
        if claims.get("iss") != "https://cloud.google.com/iap" or not claims.get("email"):
            raise ValueError("Identidad IAP inválida")
        g.user_email = claims["email"].lower()
    except Exception:
        return jsonify(error="Identidad IAP no válida."), 401


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, rule TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)")
    return connection


def parse_alert_frequency(value):
    text = str(value or "").strip().lower()
    if text in ("daily", "diaria", "diario", "cada día", "cada dia", "diariamente", "cada 24 horas"):
        return "daily"
    if text in ("weekly", "semanal", "cada semana", "semanalmente"):
        return "weekly"
    if text in ("monthly", "mensual", "cada mes", "mensualmente"):
        return "monthly"
    return None


def next_alert_run(now, frequency):
    if frequency == "daily":
        return now + timedelta(days=1)
    if frequency == "weekly":
        return now + timedelta(days=7)
    month = now.month % 12 + 1
    year = now.year + (1 if month == 1 else 0)
    day = min(now.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                        31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return now.replace(year=year, month=month, day=day)


def alert_question(rule):
    filters = rule.get("alert_filters") or "sin filtros adicionales"
    period = str(rule.get("alert_period", "")).strip()
    if period.casefold() in {"mensual", "mes", "este mes", "mes actual", "mes en curso"}:
        period = "mes natural en curso, acumulado desde el día 1 hasta la fecha más reciente con datos"
    return (f"En Ventas Oribay calcula el valor definido exactamente como '{rule['alert_metric']}'. "
            "La definición puede ser un campo existente o una combinación/fórmula de varios campos. "
            "Resuelve cada componente mediante el glosario y el modelo semántico del agente; respeta "
            "operadores, paréntesis y agregaciones que haya especificado el usuario. No sustituyas una "
            "fórmula por un campo parecido ni inventes componentes. Si la fórmula, el grano de cálculo "
            "o la agregación admiten interpretaciones materialmente distintas, explica qué falta para "
            "poder fijar la regla. Para divisiones, no devuelvas un resultado si el denominador es cero "
            "o no está disponible. "
            f"Evalúa el resultado para el periodo '{period}' y los filtros '{filters}'. Devuelve una sola "
            "fila con un único valor numérico calculado y su unidad; nombra la columna con la definición "
            "solicitada o como 'Valor calculado'. No incluyas series, unidades vendidas, operandos ni otras métricas auxiliares. "
            "No inventes ni estimes datos; si no puedes verificar el valor único, explica el motivo.")


def parse_scalar_metric(tables, metric=""):
    """Return the single-row value column that best matches the requested metric."""
    import unicodedata

    def words(value):
        normalized = unicodedata.normalize("NFKD", str(value or ""))
        normalized = "".join(char for char in normalized if not unicodedata.combining(char))
        return re.findall(r"[a-z0-9]+", normalized.lower())

    requested = [word for word in words(metric)
                 if word not in {"de", "del", "la", "el", "los", "las", "cada", "mes", "mensual", "mensuales",
                                 "euros", "euro", "eur", "valor", "metrica", "numero", "numerico"}]
    aliases = []
    for word in requested:
        if word.startswith("factur"):
            aliases.append(("factur", "importe", "revenue", "sales"))
        elif word.startswith(("venta", "revenue", "sales")):
            aliases.append(("venta", "factur", "importe", "revenue", "sales"))
        elif word.startswith(("unidad", "cantidad", "quantity")):
            aliases.append(("unidad", "cantidad", "quantity"))
        elif word.startswith("importe"):
            aliases.append(("importe", "factur", "revenue", "sales"))
        else:
            aliases.append((word,))

    parsed = []
    for table in tables or []:
        rows = []
        for line in str(table).splitlines():
            if "|" not in line:
                continue
            cells = [cell.strip().replace(chr(92) + "_", "_").replace(chr(96), "") for cell in line.strip().strip("|").split("|")]
            if cells and not all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in cells):
                rows.append(cells)
        if len(rows) < 2:
            continue
        headers, data_rows = rows[0], rows[1:]
        if len(data_rows) != 1:
            continue
        candidates = []
        for index, header in enumerate(headers):
            normalized = " ".join(words(header))
            if index >= len(data_rows[0]) or not re.search(
                    r"valor|value|resultado|result|calculad|computed|importe|facturaci[oó]n|unidades|cantidad|total|revenue|sales|margen|margin|ratio|porcent|percent|tasa|rate|diferencia|difference|saldo|balance|promedio|average|beneficio|profit|coste|costo|cost|indice|índice|variaci[oó]n",
                    header, re.I):
                continue
            score = sum(any(alias in normalized for alias in group) for group in aliases)
            candidates.append((score, index, header))
        if not candidates:
            continue
        generic = [candidate for candidate in candidates if re.search(
            r"^(?:valor|value|resultado|result|valor calculado|calculated value)(?:\s*\([^)]*\))?$",
            candidate[2].strip(), re.I)]
        if len(generic) == 1:
            best = generic
        else:
            best_score = max(candidate[0] for candidate in candidates)
            best = [candidate for candidate in candidates if candidate[0] == best_score]
            if requested and best_score == 0:
                continue
        if len(best) != 1:
            continue
        _, index, header = best[0]
        raw = re.sub(r"[^0-9,.-]", "", data_rows[0][index])
        if not re.fullmatch(r"-?\d[\d.,]*", raw):
            continue
        if "," in raw and "." in raw:
            normalized = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
        elif "," in raw:
            tail = raw.rsplit(",", 1)[-1]
            normalized = raw.replace(",", ".") if len(tail) <= 2 else raw.replace(",", "")
        elif re.fullmatch(r"-?\d{1,3}(?:\.\d{3})+", raw):
            normalized = raw.replace(".", "")
        else:
            normalized = raw
        try:
            number = float(normalized)
            if math.isfinite(number):
                parsed.append((number, header))
        except ValueError:
            pass
    return parsed[0] if len(parsed) == 1 else (None, "")

def send_alert_notification(rule, observed):
    if os.environ.get("ORIBAY_GMAIL_DWD_ENABLED") != "true":
        raise RuntimeError("El envío desde Gmail no está habilitado para alertas.")
    recipient = rule["email"]
    sender = os.environ.get("ORIBAY_ALERT_SENDER", "").strip().lower() or recipient
    credentials = alert_gmail_credentials(sender)
    metric = str(rule.get("alert_metric", "métrica")).strip().capitalize()
    raw_unit = str(rule.get("unit", "")).strip()
    currency = "eur" in raw_unit.lower() or "€" in raw_unit
    unit = "€" if currency else raw_unit
    currency_dimension = ""
    if currency and re.search(r"(?:/|\bpor\b)\s*(?:unidad(?:es)?|pieza(?:s)?|unit(?:s)?|pieces?)", raw_unit, re.I):
        currency_dimension = " / unidad"
    value = _format_alert_number(observed, currency=currency)
    threshold = _format_alert_number(rule["alert_threshold"], currency=currency)
    operator = str(rule.get("alert_operator", ">"))
    period = {"monthly": "Mes en curso", "weekly": "Semana en curso", "daily": "Día en curso"}.get(
        str(rule.get("alert_period", "")).lower(), str(rule.get("alert_period", "—")))
    filters = str(rule.get("alert_filters") or "Sin filtros adicionales")
    subject = f"Oribay · Aviso de {metric.lower()}"
    value_text = value + currency_dimension if currency else f"{value}{' ' + unit if unit else ''}"
    threshold_text = f"{operator} {threshold}{currency_dimension}" if currency else f"{operator} {threshold}{' ' + unit if unit else ''}"
    text = (f"ORIBAY | Aviso de {metric.lower()}\n\nLa {metric.lower()} ha superado el límite establecido.\n\n"
            f"Valor observado: {value_text}\nCondición: {threshold_text}\nPeriodo: {period}\nFiltros: {filters}\n\n"
            "Puedes revisar y gestionar tus avisos desde Oribay Intelligence.")
    safe_metric, safe_value = html.escape(metric.lower()), html.escape(value_text)
    safe_threshold, safe_period, safe_filters = html.escape(threshold_text), html.escape(period), html.escape(filters)
    message = EmailMessage()
    message.set_content(text)
    message.add_alternative(f"""<!doctype html><html lang="es"><body style="margin:0;background:#f2f5fa;font-family:Arial,Helvetica,sans-serif;color:#23324a">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f2f5fa;padding:32px 12px"><tr><td align="center">
      <table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;background:#fff;border:1px solid #e0e7f0;border-radius:12px;overflow:hidden">
      <tr><td style="background:#123b70;padding:23px 30px;color:#fff"><div style="font-size:23px;font-weight:800;letter-spacing:2px">ORIBAY</div><div style="font-size:10px;letter-spacing:2px;margin-top:5px;color:#c8d9f2">GROUP AUTOMOTIVE · INTELLIGENCE</div></td></tr>
      <tr><td style="padding:30px"><div style="font-size:12px;font-weight:bold;letter-spacing:1px;text-transform:uppercase;color:#2866b2">Aviso personal</div>
      <h1 style="font-size:23px;line-height:1.3;margin:10px 0 8px;color:#172b49">Se ha superado el límite de {safe_metric}</h1>
      <p style="font-size:15px;line-height:1.6;color:#53647b;margin:0 0 22px">La comprobación programada ha detectado que se cumple la condición que configuraste.</p>
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f8fd;border-radius:9px"><tr><td style="padding:18px 20px;color:#60728b;font-size:13px">VALOR OBSERVADO</td></tr><tr><td style="padding:0 20px 20px;color:#164d8c;font-size:29px;font-weight:700">{safe_value}</td></tr></table>
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:20px;font-size:14px;line-height:1.8"><tr><td style="color:#718096">Condición</td><td align="right" style="font-weight:600;color:#243449">{safe_threshold}</td></tr><tr><td style="color:#718096">Periodo</td><td align="right" style="font-weight:600;color:#243449">{safe_period}</td></tr><tr><td style="color:#718096">Filtros</td><td align="right" style="font-weight:600;color:#243449">{safe_filters}</td></tr></table>
      <p style="font-size:12px;line-height:1.6;color:#8190a3;margin:25px 0 0">Este aviso se envía cuando la condición pasa de no cumplirse a cumplirse. Puedes revisar o pausar la alerta desde Oribay Intelligence.</p></td></tr>
      <tr><td style="border-top:1px solid #e8edf4;padding:17px 30px;font-size:11px;color:#8794a5">Oribay Intelligence · Avisos automáticos de ventas</td></tr>
      </table></td></tr></table></body></html>""", subtype="html")
    message["to"], message["from"] = recipient, sender
    if sender != recipient:
        message["reply-to"] = recipient
    message["subject"] = subject
    encoded = __import__("base64").urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False).users().messages().send(
        userId="me", body={"raw": encoded}).execute()


def alert_gmail_credentials(sender):
    return gmail_credentials(sender)


def _format_alert_number(value, currency=False):
    rendered = f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{rendered} €" if currency else rendered


def evaluate_alert(rule):
    result = query_agent(alert_question(rule), user=rule.get("email"))
    observed, value_label = parse_scalar_metric(result.get("tables", []), rule.get("alert_metric", ""))
    if observed is None:
        headers = []
        for table in result.get("tables", [])[:5]:
            first_row = next((line for line in str(table).splitlines() if "|" in line), "")
            headers.append([cell.strip()[:60] for cell in first_row.strip().strip("|").split("|")[:12]])
        app.logger.warning("Alerta sin escalar numérico: métrica=%r, tablas=%s", str(rule.get("alert_metric", ""))[:80], headers)
        raise ValueError("El agente no devolvió una única celda numérica verificable para esta métrica.")
    operator = rule["alert_operator"]
    triggered = {">": observed > rule["alert_threshold"], ">=": observed >= rule["alert_threshold"],
                 "<": observed < rule["alert_threshold"], "<=": observed <= rule["alert_threshold"]}[operator]
    return {"observed_value": observed, "value_label": value_label, "triggered": triggered,
            "answer": result.get("answer", ""), "tables": result.get("tables", [])[:3]}


def run_due_alerts():
    client = firestore.Client(project=PROJECT)
    now = datetime.now(timezone.utc)
    stats = {"checked": 0, "notified": 0, "skipped": 0, "failed": 0}
    for snapshot in client.collection("oribay_alert_drafts").where("status", "==", "active").stream():
        ref = snapshot.reference
        doc = snapshot.to_dict()
        try:
            due = datetime.fromisoformat(doc.get("next_run_at", "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            due = now
        if due > now:
            stats["skipped"] += 1
            continue
        # A short lease prevents overlapping Scheduler retries from querying/sending twice.
        transaction = client.transaction()
        @firestore.transactional
        def claim(tx):
            latest = ref.get(transaction=tx)
            if not latest.exists:
                return False
            current = latest.to_dict()
            if current.get("status") != "active":
                return False
            try:
                current_due = datetime.fromisoformat(current.get("next_run_at", "").replace("Z", "+00:00"))
                if current_due > now:
                    return False
            except (TypeError, ValueError):
                pass
            try:
                locked_until = datetime.fromisoformat(current.get("worker_lock_until", "").replace("Z", "+00:00"))
                if locked_until > now:
                    return False
            except (TypeError, ValueError):
                pass
            tx.update(ref, {"worker_lock_until": (now + timedelta(minutes=8)).isoformat()})
            return True
        if not claim(transaction):
            stats["skipped"] += 1
            continue
        try:
            rule = doc.get("rule", {})
            frequency = parse_alert_frequency(rule.get("alert_frequency"))
            if not frequency:
                raise ValueError("La frecuencia de esta alerta no es compatible.")
            result = evaluate_alert(rule)
            previous_triggered = bool(doc.get("last_triggered", False))
            update = {"last_checked_at": now.isoformat(), "last_observed_value": result["observed_value"],
                      "last_error": firestore.DELETE_FIELD, "worker_lock_until": firestore.DELETE_FIELD,
                      "next_run_at": next_alert_run(now, frequency).isoformat(),
                      "last_triggered": result["triggered"]}
            if result["triggered"] and not previous_triggered:
                delivery = send_alert_notification({**rule, "unit": result["value_label"]}, result["observed_value"])
                update["last_notified_at"] = datetime.now(timezone.utc).isoformat()
                update["last_notification_id"] = delivery.get("id", "")
                stats["notified"] += 1
            ref.update(update)
            if doc.get("library_id"):
                client.collection("oribay_library").document(doc["library_id"]).update({
                    "payload.last_checked_at": now.isoformat(), "payload.last_observed_value": result["observed_value"],
                    "payload.last_triggered": result["triggered"], "payload.last_error": firestore.DELETE_FIELD})
            ref.collection("checks").document(str(uuid.uuid4())).set({**result, "created_at": now.isoformat(), "status": "threshold_crossed" if result["triggered"] else "threshold_not_crossed"})
            stats["checked"] += 1
        except Exception as error:
            app.logger.exception("Falló la ejecución de la alerta %s", snapshot.id)
            retry = datetime.now(timezone.utc)
            ref.update({"last_checked_at": retry.isoformat(), "last_error": str(error)[:500],
                        "worker_lock_until": firestore.DELETE_FIELD, "next_run_at": (retry + timedelta(minutes=15)).isoformat()})
            stats["failed"] += 1
    return stats


class SalesDataUnavailable(RuntimeError):
    pass


def has_access_refusal(text: str) -> bool:
    return bool(re.search(
        r"no\s+(?:se\s+dispone|dispongo|tenemos|tengo|tiene|hay)\s+(?:de\s+)?(?:permisos?|acceso)|"
        r"no\s+(?:está|esta)\s+accesible|permission\s+denied|access\s+denied|"
        r"insufficient\s+permissions|not\s+authorized|unauthorized|forbidden",
        str(text or ""), re.IGNORECASE))


def resolve_email_recipient(value: str) -> str:
    recipient = str(value or "").strip()
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", recipient):
        return recipient.lower()
    normalized = re.sub(r"\s+", " ", recipient).casefold()
    verified_contacts = {"lander fraga": "lander.fraga@oribay.com"}
    if normalized in verified_contacts:
        return verified_contacts[normalized]
    if "@" not in recipient and re.fullmatch(r"[\w.+-]+", recipient):
        return f"{recipient}@oribay.com"
    return ""


def query_agent(question: str, user: str = None, display_question: str = "", conversation_history=None, agent="ventas") -> dict:
    owner = (user or getattr(g, "user_email", None) or os.environ.get("ORIBAY_USER_EMAIL", "")).lower()
    if not owner:
        raise UserBigQueryAuthError("No se pudo identificar la cuenta para consultar BigQuery.")
    result = query_data_agent_as_user(owner, question, display_question, history=conversation_history, agent=agent)
    if not result.get("answer") and not result.get("charts") and not result.get("tables"):
        app.logger.error("Respuesta MCP del agente sin artefactos reconocibles")
        raise ValueError("El agente de datos no devolvió datos verificables")
    answer = result.get("answer", "")
    if has_access_refusal(answer):
        app.logger.error("Ventas Oribay informó falta de acceso a los datos; se bloquea la generación de artefactos")
        raise SalesDataUnavailable(
            "El agente indica que no puede acceder a los datos solicitados. "
            "No se ha creado ningún borrador con datos incompletos. Revisa los permisos del agente e inténtalo de nuevo.")
    return result


@app.get("/")
def home():
    return render_template("index.html", deployed=bool(IAP_AUDIENCE))


@app.get("/api/session")
def session_info():
    email = getattr(g, "user_email", None) or os.environ.get("ORIBAY_USER_EMAIL", "")
    connected = is_connected(email) if email and IAP_AUDIENCE else False
    return jsonify(email=email, authenticated=bool(email), bigquery_connected=connected,
                   drive_connected=drive_is_connected(email) if email and IAP_AUDIENCE else False,
                   bigquery_oauth_configured=bool(os.environ.get("ORIBAY_GOOGLE_OAUTH_CLIENT_ID")
                                                  and os.environ.get("ORIBAY_GOOGLE_OAUTH_CLIENT_SECRET")
                                                  and os.environ.get("ORIBAY_OAUTH_TOKEN_ENCRYPTION_KEY")))


@app.get("/auth/google/bigquery/start")
def google_bigquery_auth_start():
    owner = getattr(g, "user_email", None)
    if not owner:
        return jsonify(error="Inicia sesión en el espacio antes de conectar BigQuery."), 401
    try:
        return redirect(begin_oauth(owner, _redirect_uri(request.url_root)))
    except UserBigQueryAuthError as exc:
        return jsonify(error=str(exc)), 503


@app.get("/auth/google/bigquery/callback")
def google_bigquery_auth_callback():
    owner = getattr(g, "user_email", None)
    if not owner:
        return "Vuelve a iniciar sesión en Oribay Intelligence y conecta BigQuery de nuevo.", 401
    if request.args.get("error"):
        return "Google no completó la autorización. Puedes volver al chat e intentarlo otra vez.", 400
    try:
        finish_oauth(owner, request.args.get("state", ""), request.args.get("code", ""),
                     _redirect_uri(request.url_root))
        return redirect("/?bigquery=connected")
    except UserBigQueryAuthError as exc:
        app.logger.warning("Autorización personal de BigQuery no completada: %s", str(exc)[:300])
        return str(exc), 400


@app.post("/auth/google/bigquery/disconnect")
def google_bigquery_auth_disconnect():
    owner = getattr(g, "user_email", None)
    if not owner:
        return jsonify(error="Inicia sesión antes de cambiar esta conexión."), 401
    disconnect_oauth(owner)
    return jsonify(disconnected=True)


@app.get("/api/drive/files")
def drive_files():
    try:
        return jsonify(drive_tools.files(current_owner(), request.args.get("q", ""),
                      request.args.get("folder", ""), request.args.get("page_token", "")))
    except UserBigQueryAuthError as exc:
        return jsonify(error=str(exc)), 401
    except Exception:
        app.logger.exception("No se pudo consultar Drive")
        return jsonify(error="No se pudieron consultar los archivos de Drive."), 502


@app.get("/api/drive/files/<file_id>/content")
def drive_file_content(file_id):
    try:
        return jsonify(drive_tools.read_text(current_owner(), file_id))
    except (UserBigQueryAuthError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        app.logger.exception("No se pudo leer el archivo de Drive")
        return jsonify(error="No se pudo leer este archivo con tu autorización de Drive."), 502


@app.post("/api/drive/folders")
def drive_folder_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not data.get("confirmed") or not 1 <= len(name) <= 200:
        return jsonify(error="Confirma el nombre de la carpeta."), 400
    try:
        return jsonify(drive_tools.create_folder(current_owner(), name, data.get("parent", "")))
    except Exception:
        app.logger.exception("No se pudo crear la carpeta")
        return jsonify(error="No se pudo crear la carpeta en Drive."), 502


@app.patch("/api/drive/files/<file_id>")
def drive_file_update(file_id):
    data = request.get_json(silent=True) or {}
    if not data.get("confirmed"):
        return jsonify(error="Confirma el cambio antes de aplicarlo."), 400
    try:
        if data.get("name") and 1 <= len(str(data["name"]).strip()) <= 200:
            return jsonify(drive_tools.rename(current_owner(), file_id, str(data["name"]).strip()))
        if data.get("destination"):
            return jsonify(drive_tools.move(current_owner(), file_id, str(data["destination"])))
        return jsonify(error="Indica un nombre o carpeta de destino."), 400
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        app.logger.exception("No se pudo organizar el archivo")
        return jsonify(error="No se pudo actualizar el archivo en Drive."), 502


@app.post("/api/library/<item_id>/drive")
def library_save_to_drive(item_id):
    data = request.get_json(silent=True) or {}
    if not data.get("confirmed"):
        return jsonify(error="Confirma dónde quieres guardar el informe."), 400
    doc = firestore.Client(project=PROJECT).collection("oribay_library").document(item_id).get()
    if not doc.exists or doc.to_dict().get("owner") != current_owner() or doc.to_dict().get("deleted") or doc.to_dict().get("type") != "document":
        return jsonify(error="Informe no encontrado en tu espacio."), 404
    item = doc.to_dict()
    try:
        pdf_bytes = storage.Client(project=PROJECT).bucket(LIBRARY_BUCKET).blob(item["object_path"]).download_as_bytes()
        name = str(data.get("name") or (item.get("payload") or {}).get("filename") or "informe-oribay.pdf").strip()
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        if len(name) > 200:
            return jsonify(error="El nombre del PDF es demasiado largo."), 400
        return jsonify(drive_tools.upload_pdf(current_owner(), name, pdf_bytes, str(data.get("folder", ""))))
    except Exception:
        app.logger.exception("No se pudo guardar el informe en Drive")
        return jsonify(error="No se pudo guardar el PDF en tu Drive."), 502


@app.get("/api/conversations")
def conversations_list():
    restore_saved_analyses_as_chat_history()
    collection = firestore.Client(project=PROJECT).collection("oribay_conversations")
    items = []
    for snapshot in collection.where("owner", "==", current_owner()).stream():
        item = snapshot.to_dict()
        if item.get("deleted"):
            continue
        items.append({"id": snapshot.id, "title": item.get("title", "Nueva conversación"),
                      "summary": item.get("summary", ""), "preview": item.get("preview", ""), "updated_at": item.get("updated_at", ""),
                      "message_count": item.get("message_count", 0)})
    items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    return jsonify(items=items[:100])


@app.get("/api/agents/available")
def available_agents():
    try:
        return jsonify(agents=list_data_agents_as_user(current_owner()))
    except Exception:
        app.logger.warning("No se pudo descubrir los agentes del usuario", exc_info=True)
        return jsonify(agents=[], error="No se pudieron comprobar los agentes disponibles.")


@app.post("/api/conversations/<conversation_id>/summary")
def conversation_summary(conversation_id):
    ref, conversation = owned_conversation(conversation_id)
    if ref is None:
        return jsonify(error="No se encontró esta conversación."), 404
    messages = [snapshot.to_dict() for snapshot in ref.collection("messages").order_by("created_at").stream()]
    turns, question = [], ""
    for message in messages:
        if message.get("kind") == "user":
            question = str(message.get("question", ""))
        elif question and message.get("kind") == "assistant":
            if message.get("route") != "error":
                turns.append({"question": question, "answer": message.get("answer", "")})
            question = ""
    if not turns:
        return jsonify(summary=conversation.get("summary", ""))
    summary = summarize_conversation(turns)
    ref.update({"summary": summary, "title": summary})
    return jsonify(summary=summary)


@app.post("/api/conversations")
def conversation_create():
    conversation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    firestore.Client(project=PROJECT).collection("oribay_conversations").document(conversation_id).set({
        "owner": current_owner(), "title": "Nueva conversación", "preview": "",
        "created_at": now, "updated_at": now, "message_count": 0, "deleted": False,
    })
    return jsonify(id=conversation_id, title="Nueva conversación", updated_at=now), 201


@app.get("/api/conversations/<conversation_id>")
def conversation_get(conversation_id):
    ref, conversation = owned_conversation(conversation_id)
    if ref is None:
        return jsonify(error="No se encontró esta conversación."), 404
    messages = []
    for snapshot in ref.collection("messages").order_by("created_at").stream():
        message = snapshot.to_dict()
        email_snapshot = message.get("email_snapshot", {})
        email_library_id = message.get("email_library_id", "")
        email_invalid = has_access_refusal((email_snapshot or {}).get("body", ""))
        if email_invalid:
            email_snapshot, email_library_id = {}, ""
        messages.append({"kind": message.get("kind"), "created_at": message.get("created_at"),
                         "question": message.get("question", ""), "answer": message.get("answer", ""),
                         "route": message.get("route", ""), "data_agent": message.get("data_agent", ""),
                         "title": message.get("title", ""),
                         "library_id": message.get("library_id", ""),
                         "email_library_id": email_library_id,
                         "email_snapshot": email_snapshot, "email_invalid": email_invalid,
                         "presentation": message.get("presentation", {}),
                         "document_library_id": message.get("document_library_id", ""),
                         "charts": message.get("charts", []), "tables": message.get("tables", []),
                         "plan": message.get("plan", {})})
    return jsonify(id=conversation_id, title=conversation.get("summary") or conversation.get("title", "Chat de ventas"), messages=messages)


@app.delete("/api/conversations/<conversation_id>")
def conversation_delete(conversation_id):
    ref, _ = owned_conversation(conversation_id)
    if ref is None:
        return jsonify(error="No se encontró esta conversación."), 404
    client = firestore.Client(project=PROJECT)
    batch = client.batch()
    count = 0
    for message in ref.collection("messages").stream():
        batch.delete(message.reference)
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = client.batch()
    if count % 400:
        batch.commit()
    ref.delete()
    return jsonify(deleted=True)


@app.post("/api/ask")
def ask():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    if not 1 <= len(question) <= 2000:
        return jsonify(error="Escribe una pregunta de 1 a 2000 caracteres."), 400
    conversation_id = str(payload.get("conversation_id", "")).strip()
    conversation_ref = None
    conversation_history = []
    if conversation_id:
        conversation_ref, conversation = owned_conversation(conversation_id)
        if conversation_ref is None:
            return jsonify(error="No se encontró esta conversación."), 404
        conversation_message(conversation_ref, "user", question=question[:2000])
        if conversation.get("title") == "Nueva conversación":
            conversation_ref.update({"title": question[:72], "updated_at": datetime.now(timezone.utc).isoformat()})
    try:
        history = conversation_recent_agent_turns(conversation_ref, max_turns=5) if conversation_ref else []
        if history:
            history = [{**turn, "answer": focused_prior_answer(question, turn["answer"])} for turn in history]
        plan = decide(question, history)
        source = followup_source(question, history) if needs_previous_turn(question) else None
        if source and source["route"] == plan.get("data_agent", "ventas"):
            original = source["question"]
            plan["data_question"] = (f"Resuelve la petición actual: {question}. Su referencia es esta consulta anterior: "
                                     f"{original}. Conserva exactamente su alcance, periodo, filtros y número de resultados "
                                     "salvo que la petición actual los cambie explícitamente. Si pide un gráfico, "
                                     "devuelve un gráfico de esos mismos resultados y su tabla de respaldo. "
                                     "No cambies un top 5 por un top 10.")[:2000]
            plan["followup_source"] = original[:1200]
            conversation_history = [
                {"userMessage": {"text": original}},
                {"systemMessage": {"text": {"parts": [source["answer"]]}}},
            ]
            plan["context_used"] = True
            plan["context_reference"] = original[:180]
        result = execute_and_store(question, plan, conversation_history=conversation_history)
        if conversation_ref:
            save_conversation_result(conversation_ref, question, result)
            result["conversation_id"] = conversation_id
        return jsonify(**result)
    except Exception as exc:
        app.logger.exception("Error consultando el agente de datos")
        if conversation_ref:
            conversation_message(conversation_ref, "assistant", answer="No pude completar esta solicitud. Puedes intentarlo de nuevo.", route="error")
            conversation_ref.update({"updated_at": datetime.now(timezone.utc).isoformat(), "message_count": firestore.Increment(2)})
        message = str(exc) if isinstance(exc, (SalesDataUnavailable, ValueError)) else f"No se pudo consultar el agente de datos: {type(exc).__name__}"
        return jsonify(error=message), 502


@app.post("/api/route")
def route_request():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    if not 1 <= len(question) <= 2000:
        return jsonify(error="Escribe una pregunta de 1 a 2000 caracteres."), 400
    return jsonify(plan=decide(question, payload.get("history")))


@app.post("/api/execute")
def execute_request():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    plan = payload.get("plan") or {}
    if not 1 <= len(question) <= 2000 or plan.get("route") not in ("ventas", "adhesiva", "alerta", "pdf", "correo", "drive", "presentacion"):
        return jsonify(error="Solicitud no válida."), 400
    try:
        return jsonify(**execute_and_store(question, plan))
    except Exception as exc:
        app.logger.exception("Error ejecutando herramienta")
        message = str(exc) if isinstance(exc, SalesDataUnavailable) else f"No se pudo ejecutar la herramienta: {type(exc).__name__}"
        return jsonify(error=message), 502


def execute_plan(question: str, plan: dict, conversation_history=None) -> dict:
        route = plan["route"]
        data_agent = route if route in ("ventas", "adhesiva") else plan.get("data_agent", "ventas")
        if data_agent not in ("ventas", "adhesiva"):
            raise ValueError("Agente de datos no permitido")
        plan["data_agent"] = data_agent
        plan.setdefault("user_request", question)
        steps = [{"name": "Router", "detail": f"Ruta elegida: {route} · {plan['router_mode']}", "status": "done"}]
        if route == "alerta":
            if data_agent == "adhesiva":
                raise ValueError("Las alertas de Aplicación Adhesiva aún no están configuradas. No se guardó ninguna regla.")
            steps.append({"name": "Regla", "detail": "Revisando métrica, periodo, umbral y frecuencia", "status": "done"})
            return dict(route=route, plan=plan, steps=steps, answer=plan.get("clarification") if plan.get("needs_clarification") else "Regla preparada. Revísala antes de guardarla.")
        if route == "drive":
            steps.append({"name": "Drive", "detail": "Consultando tus archivos", "status": "done"})
            if not drive_is_connected(current_owner()):
                return dict(route=route, plan=plan, steps=steps, drive={"connected": False},
                            answer="Para trabajar con tus archivos, conecta Google Drive con tu cuenta.")
            action = plan.get("drive_action") or "search"
            path_match = re.search(r"\bpath:([^\s,;]+)", question, re.IGNORECASE)
            path = path_match.group(1).strip("/")[:160] if path_match else str(plan.get("drive_path") or "")[:160]
            query = re.sub(r"\bpath:[^\s,;]+", " ", str(plan.get("drive_query") or plan.get("drive_file") or ""), flags=re.IGNORECASE).strip()[:120]
            search_phrase = query or re.sub(r"\bpath:[^\s,;]+", " ", question, flags=re.IGNORECASE)
            result = {"connected": True, "action": action, "files": [], "documents": []}
            if action in ("search", "read"):
                found = drive_tools.search_relevant(current_owner(), search_phrase, path)
                result["sources"] = [{"id": item["id"], "name": item.get("name", "Archivo"),
                                      "url": item.get("webViewLink", ""), "mimeType": item.get("mimeType", "")}
                                     for item in found["files"][:8]]
                if not found["files"]:
                    answer = "No he encontrado archivos relacionados con esa consulta en tu Drive. Prueba con otro nombre o carpeta."
                else:
                    evidence = []
                    for item in found["files"][:6]:
                        try:
                            content = drive_tools.read_text(current_owner(), item["id"])
                            evidence.append({"name": item.get("name", ""), "text": content["text"][:7000]})
                        except Exception as exc:
                            app.logger.info("No se pudo leer un candidato de Drive: %s", type(exc).__name__)
                    answer = answer_from_drive(question, evidence) if evidence else (
                        "He encontrado archivos relacionados, pero su contenido no se puede leer aquí. Puedes abrirlos desde los enlaces de Drive.")
                    if path and not found["path_found"]:
                        answer = f"No localicé la ruta exacta «{path}»; amplié la búsqueda a tu Drive.\n\n" + answer
                steps[-1]["detail"] = f"{len(found['files'])} archivos relacionados; respuesta basada en las fuentes disponibles"
                return dict(route=route, plan=plan, steps=steps, drive=result, answer=answer)
            if action in ("rename", "move"):
                result["files"] = drive_tools.search_relevant(current_owner(), search_phrase, path)["files"]
            if action == "save_pdf":
                docs = firestore.Client(project=PROJECT).collection("oribay_library").where("owner", "==", current_owner()).stream()
                result["documents"] = [{"id": d.id, "title": d.to_dict().get("title", "Informe")}
                                       for d in docs if d.to_dict().get("type") == "document" and not d.to_dict().get("deleted")][:30]
            return dict(route=route, plan=plan, steps=steps, drive=result,
                        answer="He preparado la acción en Drive. Elige el archivo y confirma cualquier cambio.")
        if route == "presentacion" and not drive_is_connected(current_owner()):
            raise UserBigQueryAuthError("Conecta Google Drive con tu cuenta para crear una presentación.")
        data_question = plan.get("data_question") or question
        if data_agent == "adhesiva" and conversation_history:
            prior_question = conversation_history[-2]["userMessage"]["text"]
            prior_answer = conversation_history[-1]["systemMessage"]["text"]["parts"][0]
            prior_context = prior_question + " " + prior_answer
            machine = re.search(r"\b(?:m[aá]quina\s+)?(RB\d+)\b", prior_context, re.IGNORECASE)
            week = re.search(r"\bsemana\s+(\d{1,2})\s+de\s+(20\d{2})\b", prior_context, re.IGNORECASE)
            if machine and week:
                data_question += (f" La consulta anterior identificó la máquina {machine.group(1).upper()} "
                                  f"y la semana {week.group(1)} de {week.group(2)}. "
                                  "La expresión «ese resultado» se refiere a esa máquina y semana.")
        if data_agent == "adhesiva":
            data_question += (" Responde exactamente al nivel solicitado (máquina, semana o artículo). "
                              "Si se pide un OEE global de una máquina y periodo, usa la definición agregada del glosario; "
                              "no sustituyas ese valor por una lista de OEE por referencia. "
                              "Si no hay valor agregado verificable, indícalo expresamente.")
        data_question += (" Presenta las cifras con formato de España: punto para separar millares y coma para decimales "
                          "(por ejemplo, 2.083.849,69 €); conserva exactamente identificadores como AC032 y no cambies ni recalcules valores.")
        if route in ("pdf", "presentacion"):
            data_question += (
                " Devuelve un análisis detallado y verificable para un informe ejecutivo: identifica "
                "el periodo, alcance y filtros; presenta los principales hallazgos cuantitativos; compara "
                "periodos solo cuando sean comparables; explica tendencias observables sin atribuir causas "
                "no demostradas; señala límites de interpretación. Incluye las tablas de datos y un gráfico "
                "adecuado con títulos, unidades y etiquetas legibles. No inventes cifras ni definiciones."
            )
            if re.search(r"\bsolo\b", question, re.IGNORECASE):
                data_question += (" La palabra «solo» restringe el alcance: usa el historial únicamente "
                                  "para identificar referencias o filtros mencionados; excluye del informe "
                                  "cualquier cliente, planta o análisis anterior ajeno al alcance pedido.")
        data = query_agent(data_question, display_question=question, conversation_history=conversation_history, agent=data_agent)
        source_top = str(plan.get("followup_source") or "")
        if source_top and exceeds_requested_top(source_top, data.get("tables")):
            data = query_agent(data_question + " Repite la consulta con EXACTAMENTE el top solicitado; "
                               "no amplíes el número de referencias.", display_question=question,
                               agent=data_agent)
            if exceeds_requested_top(source_top, data.get("tables")):
                raise ValueError("El agente amplió el ranking solicitado. No mostraré un top distinto al pedido.")
        if route == "ventas":
            data["answer"] = reconcile_top_two_share(data.get("answer", ""), data.get("tables", []))
        if route in ("pdf", "presentacion") and missing_report_evidence(data):
            raise ValueError("El agente no ha devuelto datos suficientes para crear el documento. "
                             "Indica el alcance y periodo o vuelve a consultar.")
        if route == "presentacion":
            steps.append({"name": "Aplicación Adhesiva" if data_agent == "adhesiva" else "Ventas Oribay",
                          "detail": "Consulté el agente de datos y recibí sus resultados", "status": "done"})
            evidence = [{"agent": data_agent, "answer": data.get("answer", "")[:10000],
                         "tables": data.get("tables", [])[:5]}]
            if re.search(r"factur|ventas?|clientes?|pedido", question, re.IGNORECASE) and re.search(
                    r"adhesiv|\boee\b|producci[oó]n|\bRB\d+\b", question, re.IGNORECASE):
                second = "adhesiva" if data_agent == "ventas" else "ventas"
                other = query_agent(question + " Responde solo con datos de tu ámbito; no infieras relaciones causales con el otro agente.",
                                    display_question=question, agent=second)
                if missing_report_evidence(other):
                    raise ValueError("El segundo agente no devolvió datos suficientes para la presentación.")
                evidence.append({"agent": second, "answer": other.get("answer", "")[:10000],
                                 "tables": other.get("tables", [])[:5]})
                steps.append({"name": "Aplicación Adhesiva" if second == "adhesiva" else "Ventas Oribay",
                              "detail": "Consulté el segundo agente de datos", "status": "done"})
            deck = create_presentation(current_owner(), question, evidence)
            data["presentation"] = deck
            data["answer"] = (f"Presentación editable creada en tu Google Drive: {deck['title']}. "
                              f"Contiene {deck['slides']} diapositivas basadas en los datos consultados."
                              + (f"\n\nLímite de interpretación: {deck['limitation']}" if deck["limitation"] else ""))
            steps.append({"name": "Google Slides", "detail": "Presentación creada en tu Drive", "status": "done"})
        if route == "pdf" and re.search(r"[uú]ltima\s+consulta", question, re.IGNORECASE) and conversation_history:
            prior_answer = conversation_history[-1]["systemMessage"]["text"]["parts"][0]
            expected_refs = {code for code in re.findall(r"`([A-Za-z0-9.-]{4,})`", prior_answer)
                             if re.search(r"\d", code)}
            current_text = data.get("answer", "") + " " + " ".join(data.get("tables", []))
            missing = {code for code in expected_refs if code.casefold() not in current_text.casefold()}
            if missing:
                retry_question = data_question + (
                    " La consulta anterior contenía estas referencias verificadas: "
                    + ", ".join(sorted(expected_refs)) +
                    ". Incluye todas las referencias dentro del alcance solicitado y sus plantas; "
                    "no reduzcas el informe a una sola referencia.")
                data = query_agent(retry_question, display_question=question,
                                   conversation_history=conversation_history, agent=data_agent)
                current_text = data.get("answer", "") + " " + " ".join(data.get("tables", []))
                missing = {code for code in expected_refs if code.casefold() not in current_text.casefold()}
                if missing:
                    raise ValueError("No pude verificar todas las referencias de la última consulta "
                                     "para el PDF. No generaré un informe incompleto; reformula el alcance.")
        if route != "presentacion":
            steps.append({"name": "Aplicación Adhesiva" if data_agent == "adhesiva" else "Ventas Oribay", "detail": "Consulté el agente de datos y recibí sus resultados", "status": "done"})
        if route == "pdf":
            data["source_answer"] = data.get("answer", "")
            data["answer"] = draft_pdf_analysis(question, data.get("answer", ""), data.get("tables", []), data_agent=data_agent)
            steps.append({"name": "PDF", "detail": "Documento preparado para descargar", "status": "done"})
        elif route == "correo":
            steps.append({"name": "Gmail", "detail": "Mensaje preparado para revisión; todavía no se ha enviado", "status": "done"})
            recipient = resolve_email_recipient(plan.get("recipient")) or current_owner()
            draft = draft_business_email(question, data.get("answer", ""), plan.get("subject") or "", data_agent=data_agent)
            data["email"] = {"to": recipient, **draft, "sender": current_owner()}
        return dict(route=route, data_agent=data_agent, plan=plan, steps=steps, **data)


def execute_and_store(question: str, plan: dict, conversation_history=None) -> dict:
    result = execute_plan(question, plan, conversation_history=conversation_history)
    route = result.get("route")
    if route in ("ventas", "adhesiva", "pdf", "correo", "presentacion") and (result.get("answer") or result.get("charts") or result.get("tables")):
        saved_charts = result.get("charts", [])[:6]
        while saved_charts and len(json.dumps(saved_charts, ensure_ascii=False).encode("utf-8")) > 650_000:
            saved_charts.pop()
        result["library_id"] = save_library_item("analysis", question[:180], {
            "question": question[:2000], "answer": result.get("answer", "")[:12000],
            "charts": saved_charts, "tables": result.get("tables", [])[:6], "route": route,
            "data_agent": result.get("data_agent", "ventas")})
    if route == "presentacion" and result.get("presentation"):
        result["presentation"]["library_id"] = save_library_item(
            "presentation", result["presentation"]["title"],
            {"question": question[:2000], **result["presentation"]})
    if route == "correo" and result.get("email"):
        mail = result["email"]
        result["email"]["library_id"] = save_library_item("email", mail["subject"], {**mail, "status": "draft"})
    return result


@app.post("/api/pdf")
def pdf():
    data = request.get_json(silent=True) or {}
    data_agent = data.get("data_agent") if data.get("data_agent") in ("ventas", "adhesiva") else "ventas"
    source_name = "Aplicación Adhesiva" if data_agent == "adhesiva" else "Ventas Oribay"
    answer = str(data.get("answer", ""))[:12000]
    charts = data.get("charts") or []
    tables = data.get("tables") or []
    title = str(data.get("title") or ("Informe de Aplicación Adhesiva" if data_agent == "adhesiva" else "Informe comercial Oribay")).strip()[:180]
    question = str(data.get("question") or "").strip()[:2000]
    total_image_bytes = sum(len(chart.get("image", "")) for chart in charts[:6] if isinstance(chart, dict))
    if total_image_bytes > 12_000_000:
        return jsonify(error="Los gráficos superan el tamaño permitido para un PDF. Reduce su número o resolución."), 413
    if not answer and not charts and not tables:
        return jsonify(error="No hay contenido verificado para el PDF."), 400
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=25, leading=31, textColor=colors.HexColor("#183153"), alignment=0, spaceAfter=9))
    styles.add(ParagraphStyle(name="ReportSubtitle", parent=styles["BodyText"], fontName="Helvetica", fontSize=10, leading=15, textColor=colors.HexColor("#60738b"), spaceAfter=5))
    styles.add(ParagraphStyle(name="ReportBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=9.5, leading=14.5, textColor=colors.HexColor("#26384d"), spaceAfter=6, splitLongWords=1))
    styles.add(ParagraphStyle(name="ReportH1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, leading=21, textColor=colors.HexColor("#183153"), spaceBefore=13, spaceAfter=7, keepWithNext=True))
    styles.add(ParagraphStyle(name="ReportH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=12, leading=16, textColor=colors.HexColor("#245b9e"), spaceBefore=9, spaceAfter=5, keepWithNext=True))
    styles.add(ParagraphStyle(name="ReportMeta", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.5, leading=12, textColor=colors.HexColor("#718198"), spaceAfter=4))
    styles.add(ParagraphStyle(name="ReportBullet", parent=styles["ReportBody"], leftIndent=12, firstLineIndent=-8, bulletIndent=0, spaceAfter=4))
    styles.add(ParagraphStyle(name="ReportTableCell", parent=styles["ReportMeta"], fontSize=8, leading=10.5, textColor=colors.HexColor("#354a63")))
    styles.add(ParagraphStyle(name="ReportTableHeader", parent=styles["ReportTableCell"], fontName="Helvetica-Bold", textColor=colors.HexColor("#183153")))
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=21*mm, leftMargin=21*mm, topMargin=21*mm, bottomMargin=19*mm, title=title, author="Oribay Intelligence", subject=question[:250])
    import html
    logo_path = os.path.join(os.path.dirname(__file__), "static", "oribay-logo.png")
    story = []
    if os.path.isfile(logo_path):
        logo_width, logo_height = ImageReader(logo_path).getSize()
        max_logo_width, max_logo_height = 40*mm, 14*mm
        scale = min(max_logo_width/logo_width, max_logo_height/logo_height)
        story.extend([Image(logo_path, width=logo_width*scale, height=logo_height*scale), Spacer(1, 7*mm)])
    story.extend([Paragraph("ORIBAY GROUP  /  INTELLIGENCE WORKSPACE", styles["ReportMeta"]),
             Spacer(1, 4*mm), Paragraph(html.escape(title), styles["ReportTitle"]),
             Paragraph("ANÁLISIS  ·  " + source_name.upper(), styles["ReportSubtitle"]),
             Spacer(1, 2*mm), Paragraph(f"Preparado el {datetime.now().astimezone().strftime('%d/%m/%Y · %H:%M')}", styles["ReportMeta"])])
    if question:
        story.extend([Spacer(1, 4*mm), Paragraph("SOLICITUD", styles["ReportMeta"]), Paragraph(html.escape(question), styles["ReportBody"])])
    story.append(Spacer(1, 5*mm))

    def inline_markup(value):
        escaped = html.escape(value.strip())
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", escaped)
        escaped = re.sub(r"`([^`]+)`", r"<font name='Courier'>\1</font>", escaped)
        return escaped

    for line in answer.splitlines():
        clean = line.strip()
        if not clean:
            story.append(Spacer(1, 2))
        elif clean.startswith("# "):
            story.append(Paragraph(inline_markup(clean[2:]), styles["ReportH1"]))
        elif clean.startswith("## ") or clean.startswith("### "):
            story.append(Paragraph(inline_markup(clean[3:] if clean.startswith("## ") else clean[4:]), styles["ReportH2"]))
        elif re.match(r"^[-*•]\s+", clean):
            story.append(KeepTogether([Paragraph("• " + inline_markup(re.sub(r"^[-*•]\s+", "", clean)), styles["ReportBullet"])]))
        elif re.match(r"^\d+[.)]\s+", clean):
            story.append(KeepTogether([Paragraph(inline_markup(clean), styles["ReportBullet"])]))
        else:
            story.append(Paragraph(inline_markup(clean), styles["ReportBody"]))
    for index, raw_table in enumerate(tables, start=1):
        if not isinstance(raw_table, str) or not raw_table.strip():
            continue
        rows=[]
        headers=[]
        for line in raw_table.splitlines():
            cells=[cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells)>1 and not all(re.fullmatch(r"[-:\s]+", c or "-") for c in cells):
                is_header = not rows
                if is_header:
                    headers = cells
                rendered=[]
                for cell_index, cell in enumerate(cells):
                    value = re.sub(r"\\([\\`*_{}\[\]()#+.!|>-])", r"\1", cell).strip()
                    header = re.sub(r"\\_", "_", headers[cell_index]).casefold() if headers and cell_index < len(headers) else ""
                    if is_header:
                        key = value.casefold().replace("\\_", "_")
                        known = {"fecha_mes": "Fecha", "facturacion_eur": "Facturación (€)",
                                 "facturacion_piezas_eur": "Facturación de piezas (€)",
                                 "facturacion_portes_eur": "Portes (€)", "unidades": "Unidades"}
                        value = known.get(key, value.replace("_", " "))
                        if key not in known and "eur" in key:
                            value = value.replace(" EUR", " (€)")
                        style = styles["ReportTableHeader"]
                    else:
                        try:
                            number = float(value.replace(",", "."))
                            if any(term in header for term in ("eur", "importe", "facturacion", "precio")):
                                value = f"{number:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
                            elif "unidad" in header and number.is_integer():
                                value = f"{int(number):,}".replace(",", ".")
                        except (ValueError, OverflowError):
                            pass
                        style = styles["ReportTableCell"]
                    rendered.append(Paragraph(html.escape(value), style))
                rows.append(rendered)
        if rows:
            column_count = len(rows[0])
            story.extend([Paragraph(f"Tabla de datos {index}", styles["ReportH2"]), Table(rows, colWidths=[doc.width / column_count] * column_count, repeatRows=1, hAlign="LEFT", splitByRow=1, style=TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e8f0fb")),("TEXTCOLOR",(0,0),(-1,0),colors.HexColor("#183153")),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#d5dfeb")),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f7f9fc")]),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)])), Spacer(1,5*mm)])
    if charts:
        story.append(PageBreak())
        story.append(Paragraph("Gráficos", styles["ReportH1"]))
        for index, chart in enumerate(charts[:6], start=1):
            image_data = chart.get("image") if isinstance(chart, dict) else None
            if not isinstance(image_data, str) or not image_data.startswith("data:image/png;base64,"):
                continue
            import base64
            try:
                image_bytes=base64.b64decode(image_data.split(",",1)[1], validate=True)
                if len(image_bytes)>5_000_000:
                    continue
                reader=ImageReader(io.BytesIO(image_bytes)); width,height=reader.getSize()
                max_width,max_height=170*mm,105*mm
                scale=min(max_width/width,max_height/height,1)
                story.extend([Paragraph(html.escape(str(chart.get("title") or f"Gráfico {index}")), styles["ReportH2"]), Image(io.BytesIO(image_bytes),width=width*scale,height=height*scale), Spacer(1,7*mm)])
            except Exception:
                app.logger.warning("Se omitió un gráfico no válido del PDF")
    def footer(page_canvas, document):
        page_canvas.saveState(); page_canvas.setStrokeColor(colors.HexColor("#00338a")); page_canvas.setLineWidth(1.2); page_canvas.line(21*mm,15*mm,A4[0]-21*mm,15*mm); page_canvas.setFont("Helvetica",8); page_canvas.setFillColor(colors.HexColor("#526b89")); page_canvas.drawString(21*mm,10*mm,"ORIBAY GROUP  ·  Fuente: " + source_name); page_canvas.drawRightString(A4[0]-21*mm,10*mm,f"{document.page}"); page_canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    pdf_bytes = output.getvalue()
    object_path = f"{current_owner()}/{uuid.uuid4()}.pdf"
    storage.Client(project=PROJECT).bucket(LIBRARY_BUCKET).blob(object_path).upload_from_string(pdf_bytes, content_type="application/pdf")
    item_id = save_library_item("document", title, {"mime_type": "application/pdf", "filename": "informe-ventas-oribay.pdf", "summary": answer[:1200], "question": question}, object_path)
    output = io.BytesIO(pdf_bytes)
    response = send_file(output, mimetype="application/pdf", as_attachment=True, download_name="informe-ventas-oribay.pdf")
    response.headers["X-Oribay-Library-Id"] = item_id
    return response


@app.get("/api/library")
def library_list():
    kind = request.args.get("type", "")
    query = firestore.Client(project=PROJECT).collection("oribay_library").where("owner", "==", current_owner())
    if kind in ("analysis", "document", "email", "alert"):
        query = query.where("type", "==", kind)
    items = []
    for doc in query.stream():
        item = doc.to_dict()
        if item.get("deleted"):
            continue
        payload = item.get("payload") or {}
        items.append({"id": doc.id, "type": item.get("type"), "title": item.get("title"),
                      "created_at": item.get("created_at"), "summary": payload.get("answer", payload.get("summary", payload.get("body", "")))[:280],
                      "question": payload.get("question", ""), "downloadable": bool(item.get("object_path")), "alert_id": payload.get("alert_id", ""), "status": payload.get("status", "")})
    if kind in ("", "alert"):
        represented = {item.get("alert_id") for item in items if item.get("alert_id")}
        alerts = firestore.Client(project=PROJECT).collection("oribay_alert_drafts").where("owner", "==", current_owner()).stream()
        for alert_doc in alerts:
            alert = alert_doc.to_dict()
            if alert.get("status") == "deleted" or alert_doc.id in represented:
                continue
            rule = alert.get("rule", {})
            items.append({"id": alert_doc.id, "alert_id": alert_doc.id, "type": "alert",
                          "title": f"{rule.get('alert_metric','Alerta')} {rule.get('alert_operator','')} {rule.get('alert_threshold','')}",
                          "created_at": alert.get("created_at", ""),
                          "summary": f"{rule.get('alert_period','')} · {rule.get('alert_frequency','')}",
                          "question": "", "downloadable": False, "status": alert.get("status", "draft")})
    items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return jsonify(items=items[:200])


@app.get("/api/library/<item_id>")
def library_item(item_id):
    ref = firestore.Client(project=PROJECT).collection("oribay_library").document(item_id)
    doc = ref.get()
    if not doc.exists or doc.to_dict().get("owner") != current_owner() or doc.to_dict().get("deleted"):
        return jsonify(error="Elemento no encontrado."), 404
    item = doc.to_dict()
    if item.get("type") == "email" and has_access_refusal((item.get("payload") or {}).get("body", "")):
        return jsonify(error="Este borrador contiene una respuesta de falta de acceso y se ha bloqueado."), 409
    return jsonify(id=doc.id, type=item.get("type"), title=item.get("title"), created_at=item.get("created_at"), payload=item.get("payload", {}))


@app.get("/api/library/<item_id>/download")
def library_download(item_id):
    ref = firestore.Client(project=PROJECT).collection("oribay_library").document(item_id)
    doc = ref.get()
    if not doc.exists or doc.to_dict().get("owner") != current_owner() or doc.to_dict().get("deleted"):
        return jsonify(error="Elemento no encontrado."), 404
    item = doc.to_dict()
    path = item.get("object_path")
    if not path:
        return jsonify(error="Este elemento no tiene archivo adjunto."), 404
    blob = storage.Client(project=PROJECT).bucket(LIBRARY_BUCKET).blob(path)
    return send_file(io.BytesIO(blob.download_as_bytes()), mimetype="application/pdf", as_attachment=True, download_name=(item.get("payload") or {}).get("filename", "informe.pdf"))


@app.delete("/api/library/<item_id>")
def library_delete(item_id):
    ref = firestore.Client(project=PROJECT).collection("oribay_library").document(item_id)
    doc = ref.get()
    if not doc.exists or doc.to_dict().get("owner") != current_owner() or doc.to_dict().get("deleted"):
        return jsonify(error="Elemento no encontrado."), 404
    item = doc.to_dict()
    if item.get("object_path"):
        storage.Client(project=PROJECT).bucket(LIBRARY_BUCKET).blob(item["object_path"]).delete()
    ref.update({"deleted": True, "deleted_at": datetime.now(timezone.utc).isoformat()})
    return jsonify(deleted=True)


@app.patch("/api/library/<item_id>")
def library_update(item_id):
    ref = firestore.Client(project=PROJECT).collection("oribay_library").document(item_id)
    doc = ref.get()
    if not doc.exists or doc.to_dict().get("owner") != current_owner() or doc.to_dict().get("deleted"):
        return jsonify(error="Elemento no encontrado."), 404
    item = doc.to_dict()
    if item.get("type") != "email":
        return jsonify(error="Este elemento no se puede editar aquí."), 400
    payload = request.get_json(silent=True) or {}
    old = item.get("payload", {})
    if old.get("status") != "draft":
        return jsonify(error="Solo se pueden editar borradores."), 409
    to = str(payload.get("to", old.get("to", ""))).strip()
    subject = str(payload.get("subject", old.get("subject", ""))).strip()
    body = str(payload.get("body", old.get("body", ""))).strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", to) or len(to) > 254:
        return jsonify(error="Introduce una dirección de correo válida."), 400
    if not subject or len(subject) > 250 or not body or len(body) > 30000:
        return jsonify(error="El asunto y el mensaje son obligatorios; revisa su longitud."), 400
    updated = {**old, "to": to, "subject": subject, "body": body, "updated_at": datetime.now(timezone.utc).isoformat()}
    ref.update({"title": subject, "payload": updated})
    return jsonify(id=item_id, payload=updated)


@app.get("/api/email/options")
def email_options():
    choices = [{"id": "user", "label": f"Mi correo · {current_owner()}", "email": current_owner()}]
    sales_sender = os.environ.get("ORIBAY_SALES_SENDER", "").strip().lower()
    if sales_sender and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", sales_sender):
        choices.append({"id": "sales", "label": f"Buzón de ventas · {sales_sender}", "email": sales_sender})
    return jsonify(senders=choices, gmail_configured=bool(os.environ.get("ORIBAY_GMAIL_DWD_ENABLED") == "true"))


@app.post("/api/email/send")
def email_send():
    payload = request.get_json(silent=True) or {}
    if payload.get("confirmed") is not True:
        return jsonify(error="Confirma el envío desde la revisión del correo."), 400
    to = str(payload.get("to", "")).strip()
    subject = str(payload.get("subject", "")).strip()
    body = str(payload.get("body", "")).strip()
    sender_type = payload.get("sender_type", "user")
    sender = current_owner() if sender_type == "user" else os.environ.get("ORIBAY_SALES_SENDER", "").strip().lower()
    if sender_type not in ("user", "sales") or not sender or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", sender):
        return jsonify(error="El remitente seleccionado no está configurado."), 400
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", to) or len(to) > 254 or not subject or len(subject) > 250 or not body or len(body) > 30000:
        return jsonify(error="Revisa destinatario, asunto y cuerpo antes de enviar."), 400
    if has_access_refusal(body):
        return jsonify(error="Este borrador contiene un aviso de falta de acceso a los datos y no puede enviarse como correo comercial."), 409
    if os.environ.get("ORIBAY_GMAIL_DWD_ENABLED") != "true":
        return jsonify(error="Gmail aún no está autorizado para enviar desde Cloud Run. Falta habilitar la delegación de Workspace para el remitente."), 503
    try:
        credentials = gmail_credentials(sender)
        message = EmailMessage()
        message.set_content(body)
        message.add_alternative(branded_email_html(subject, body), subtype="html")
        message["to"], message["from"], message["subject"] = to, sender, subject
        encoded = __import__("base64").urlsafe_b64encode(message.as_bytes()).decode("ascii")
        sent = build("gmail", "v1", credentials=credentials, cache_discovery=False).users().messages().send(userId="me", body={"raw": encoded}).execute()
    except Exception:
        app.logger.exception("No se pudo enviar el correo desde Gmail")
        return jsonify(error="Gmail rechazó el envío. Comprueba la delegación y permisos del remitente."), 502
    item_id = save_library_item("email", subject, {"to": to, "sender": sender, "subject": subject, "body": body, "status": "sent", "gmail_message_id": sent.get("id", "")})
    return jsonify(sent=True, sender=sender, to=to, library_id=item_id)


def branded_email_html(subject, body):
    """Render an editable business email inside a responsive Oribay frame."""
    safe_subject = html.escape(str(subject or "Mensaje de Oribay"))
    paragraphs = []
    for block in re.split(r"\n\s*\n", str(body or "").strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if all(re.match(r"^(?:[-*•])\s+", line) for line in lines):
            items = "".join(
                "<li style=\"margin:0 0 8px;padding-left:3px\">" +
                re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(re.sub(r"^(?:[-*•])\s+", "", line))) +
                "</li>" for line in lines)
            paragraphs.append("<ul style=\"margin:8px 0 18px;padding-left:22px\">" + items + "</ul>")
        else:
            safe_lines = [re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(line)) for line in lines]
            paragraphs.append("<p style=\"margin:0 0 16px;line-height:1.7;color:#33465d;font-size:15px\">" +
                              "<br>".join(safe_lines) + "</p>")
    content = "".join(paragraphs)
    return f"""<!doctype html><html lang="es"><body style="margin:0;padding:0;background:#f1f4f9;font-family:Arial,Helvetica,sans-serif;color:#23324a">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f1f4f9;padding:30px 12px"><tr><td align="center">
      <table role="presentation" width="780" cellspacing="0" cellpadding="0" style="width:100%;max-width:780px;background:#fff;border:1px solid #e0e7f0;border-radius:12px;overflow:hidden">
      <tr><td style="background:#123b70;padding:22px 30px;color:#fff"><div style="font-size:23px;font-weight:800;letter-spacing:2px">ORIBAY</div><div style="font-size:10px;letter-spacing:2px;margin-top:5px;color:#c8d9f2">GROUP AUTOMOTIVE · INTELLIGENCE</div></td></tr>
      <tr><td style="padding:30px"><div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#2866b2">Comunicación comercial</div>
      <h1 style="font-size:23px;line-height:1.35;margin:9px 0 24px;color:#172b49">{safe_subject}</h1>
      <div>{content}</div></td></tr>
      <tr><td style="border-top:1px solid #e8edf4;padding:16px 30px;font-size:11px;line-height:1.5;color:#8794a5">Mensaje preparado con Oribay Intelligence</td></tr>
      </table></td></tr></table></body></html>"""


@app.post("/api/alerts")
def save_alert():
    rule = request.get_json(silent=True) or {}
    email = str(rule.get("email", "")).strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
        return jsonify(error="Introduce un correo válido para la alerta."), 400
    if rule.get("needs_clarification") or any(rule.get(k) in (None, "") for k in ("alert_metric", "alert_operator", "alert_threshold", "alert_period", "alert_frequency")):
        return jsonify(error="El aviso necesita un valor o fórmula, una condición, un límite, un periodo y una frecuencia."), 400
    if rule["alert_operator"] not in (">", ">=", "<", "<="):
        return jsonify(error="Operador no válido."), 400
    try:
        threshold = float(rule["alert_threshold"])
    except (TypeError, ValueError):
        return jsonify(error="Umbral no válido."), 400
    if not math.isfinite(threshold):
        return jsonify(error="El umbral debe ser un número finito."), 400
    alert_id = str(uuid.uuid4())
    metric = str(rule.get("alert_metric", "")).strip()
    period = str(rule.get("alert_period", "")).strip()
    if len(metric) > 500 or len(period) > 160 or not parse_alert_frequency(rule.get("alert_frequency")):
        return jsonify(error="Indica un valor o fórmula y un periodo válidos, y una frecuencia diaria, semanal o mensual."), 400
    clean = {k: rule.get(k, "") for k in ("alert_metric", "alert_operator", "alert_period", "alert_frequency", "alert_filters")}
    clean["alert_metric"], clean["alert_period"] = metric, period
    clean["alert_frequency"] = parse_alert_frequency(rule["alert_frequency"])
    clean["alert_threshold"] = threshold
    clean["email"] = email
    clean["alert_filters"] = rule.get("alert_filters") if isinstance(rule.get("alert_filters"), (str, list, dict)) else ""
    owner = getattr(g, "user_email", None) or email
    if IAP_AUDIENCE:
        if email != owner:
            return jsonify(error="El destinatario debe ser el usuario autenticado en este piloto."), 403
        library_id = save_library_item("alert", f"{clean['alert_metric']} {clean['alert_operator']} {threshold:g}", {"rule": clean, "status": "draft", "alert_id": alert_id, "summary": f"{clean['alert_period']} · {clean['alert_frequency']}"})
        firestore.Client(project=PROJECT).collection("oribay_alert_drafts").document(alert_id).set(
            {"owner": owner, "rule": clean, "status": "draft", "library_id": library_id, "created_at": datetime.now(timezone.utc).isoformat()})
    else:
        with db() as connection:
            connection.execute("INSERT INTO alerts VALUES (?, ?, ?, ?)", (alert_id, json.dumps(clean), "draft", datetime.now(timezone.utc).isoformat()))
    return jsonify(id=alert_id, status="draft", rule=clean), 201


@app.get("/api/alerts")
def list_alerts():
    if IAP_AUDIENCE:
        documents = firestore.Client(project=PROJECT).collection("oribay_alert_drafts").where("owner", "==", g.user_email).stream()
        return jsonify(alerts=[{"id": doc.id, **doc.to_dict()} for doc in documents])
    with db() as connection:
        rows = connection.execute("SELECT id, rule, status, created_at FROM alerts ORDER BY created_at DESC").fetchall()
    return jsonify(alerts=[{"id": row[0], "rule": json.loads(row[1]), "status": row[2], "created_at": row[3]} for row in rows])


def owned_alert(alert_id):
    ref = firestore.Client(project=PROJECT).collection("oribay_alert_drafts").document(alert_id)
    snapshot = ref.get()
    if not snapshot.exists or snapshot.to_dict().get("owner") != current_owner() or snapshot.to_dict().get("status") == "deleted":
        return None, None
    return ref, snapshot.to_dict()


@app.get("/api/alerts/<alert_id>")
def get_alert(alert_id):
    _, alert = owned_alert(alert_id)
    if not alert:
        return jsonify(error="Alerta no encontrada."), 404
    return jsonify(id=alert_id, rule=alert.get("rule", {}), status=alert.get("status", "draft"), created_at=alert.get("created_at", ""))


@app.patch("/api/alerts/<alert_id>")
def update_alert(alert_id):
    ref, alert = owned_alert(alert_id)
    if not ref:
        return jsonify(error="Alerta no encontrada."), 404
    rule = request.get_json(silent=True) or {}
    old = alert["rule"]
    metric = str(rule.get("alert_metric", old.get("alert_metric", ""))).strip()
    operator = str(rule.get("alert_operator", old.get("alert_operator", "")))
    period = str(rule.get("alert_period", old.get("alert_period", ""))).strip()
    frequency = str(rule.get("alert_frequency", old.get("alert_frequency", ""))).strip()
    filters = rule.get("alert_filters", old.get("alert_filters", ""))
    try:
        threshold = float(rule.get("alert_threshold", old.get("alert_threshold")))
    except (TypeError, ValueError):
        return jsonify(error="El umbral debe ser numérico."), 400
    canonical_frequency = parse_alert_frequency(frequency)
    if not metric or len(metric) > 500 or operator not in (">", ">=", "<", "<=") or not period or len(period) > 160 or not canonical_frequency:
        return jsonify(error="Revisa el valor o fórmula, la condición, el periodo y la frecuencia."), 400
    if not math.isfinite(threshold):
        return jsonify(error="El umbral debe ser un número finito."), 400
    updated = {**old, "alert_metric": metric, "alert_operator": operator, "alert_threshold": threshold, "alert_period": period, "alert_frequency": canonical_frequency, "alert_filters": filters}
    new_status = "draft" if alert.get("status") == "active" else alert.get("status", "draft")
    ref.update({"rule": updated, "status": new_status, "updated_at": datetime.now(timezone.utc).isoformat(),
                "next_run_at": firestore.DELETE_FIELD, "last_triggered": False})
    if alert.get("library_id"):
        firestore.Client(project=PROJECT).collection("oribay_library").document(alert["library_id"]).update({"title": f"{metric} {operator} {threshold:g}", "payload.rule": updated, "payload.status": new_status})
    return jsonify(id=alert_id, rule=updated, status=new_status)


@app.get("/api/alerts/config")
def alert_config():
    return jsonify(scheduled=True, email_configured=os.environ.get("ORIBAY_GMAIL_DWD_ENABLED") == "true",
                   frequencies=["daily", "weekly", "monthly"])


@app.post("/api/alerts/<alert_id>/activate")
def activate_alert(alert_id):
    ref, alert = owned_alert(alert_id)
    if not ref:
        return jsonify(error="Alerta no encontrada."), 404
    if (request.get_json(silent=True) or {}).get("confirmed") is not True:
        return jsonify(error="Confirma expresamente la activación y el envío de avisos por correo."), 400
    if os.environ.get("ORIBAY_GMAIL_DWD_ENABLED") != "true":
        return jsonify(error="El envío automático de Gmail aún no está habilitado. La alerta sigue guardada como borrador."), 409
    rule = alert.get("rule", {})
    frequency = parse_alert_frequency(rule.get("alert_frequency"))
    if not frequency:
        return jsonify(error="La frecuencia debe ser diaria, semanal o mensual."), 400
    try:
        alert_gmail_credentials(rule["email"])
    except Exception:
        app.logger.exception("La cuenta no puede autorizar el envío automático de alertas")
        return jsonify(error="Workspace aún no permite enviar alertas desde tu cuenta. La regla permanece como borrador."), 409
    try:
        preflight = evaluate_alert(rule)
    except Exception:
        app.logger.exception("No se pudo validar la métrica antes de activar la alerta")
        return jsonify(error="Ventas Oribay no devolvió un único valor verificable para esa métrica y periodo. Revisa la regla; sigue como borrador."), 422
    now = datetime.now(timezone.utc)
    ref.update({"status": "active", "activated_at": now.isoformat(), "next_run_at": now.isoformat(),
                "last_triggered": False, "last_preflight_at": now.isoformat(),
                "last_observed_value": preflight["observed_value"], "last_error": firestore.DELETE_FIELD})
    if alert.get("library_id"):
        firestore.Client(project=PROJECT).collection("oribay_library").document(alert["library_id"]).update({"payload.status": "active"})
    return jsonify(id=alert_id, status="active", next_check_at=now.isoformat(),
                   message="Alerta activada. Se comprobará en el siguiente ciclo programado y enviará un único aviso por cada nuevo cruce del umbral.")


@app.post("/api/alerts/<alert_id>/pause")
def pause_alert(alert_id):
    ref, alert = owned_alert(alert_id)
    if not ref:
        return jsonify(error="Alerta no encontrada."), 404
    paused = alert.get("status") != "paused"
    status = "paused" if paused else "draft"
    ref.update({"status": status, "updated_at": datetime.now(timezone.utc).isoformat()})
    if alert.get("library_id"):
        firestore.Client(project=PROJECT).collection("oribay_library").document(alert["library_id"]).update({"payload.status": status})
    return jsonify(id=alert_id, status=status, message="Alerta pausada." if paused else "Alerta devuelta a borrador; vuelve a activarla para programar comprobaciones.")


@app.delete("/api/alerts/<alert_id>")
def delete_alert(alert_id):
    ref, alert = owned_alert(alert_id)
    if not ref:
        return jsonify(error="Alerta no encontrada."), 404
    ref.update({"status": "deleted", "deleted_at": datetime.now(timezone.utc).isoformat()})
    if alert.get("library_id"):
        firestore.Client(project=PROJECT).collection("oribay_library").document(alert["library_id"]).update({"deleted": True, "deleted_at": datetime.now(timezone.utc).isoformat()})
    return jsonify(deleted=True)


@app.post("/api/alerts/<alert_id>/check")
def check_alert_now(alert_id):
    ref, alert = owned_alert(alert_id)
    if not ref:
        return jsonify(error="Alerta no encontrada."), 404
    rule = alert["rule"]
    try:
        result = evaluate_alert(rule)
        record = {"created_at": datetime.now(timezone.utc).isoformat(), **result,
                  "threshold": rule["alert_threshold"], "operator": rule["alert_operator"],
                  "status": "threshold_crossed" if result["triggered"] else "threshold_not_crossed"}
        check_id = str(uuid.uuid4())
        ref.collection("checks").document(check_id).set(record)
        return jsonify(id=check_id, result=record, note="Comprobación puntual completada. No activa ni modifica la programación de la alerta.")
    except Exception:
        app.logger.exception("Falló la comprobación manual de alerta")
        return jsonify(error="No se pudo obtener un valor numérico inequívoco de Ventas Oribay para esta alerta."), 502


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")))
