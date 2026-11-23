"""Classify requests and prepare actions without executing irreversible effects."""
import json
import logging
import os
import re

from google.genai import Client, types
from google.oauth2.credentials import Credentials

ROUTES = {"ventas", "alerta", "pdf", "correo"}
SYSTEM = """Eres el router de Oribay. Devuelve SOLO JSON con:
route (ventas|alerta|pdf|correo), data_question, title, recipient, subject,
alert_metric, alert_operator (>|>=|<|<=), alert_threshold (número o null),
alert_period, alert_frequency, alert_filters, needs_clarification, clarification,
context_used (boolean), context_reference (texto breve).
Elige una ruta principal. Para PDF y correo, data_question pide a Ventas Oribay
los datos estrictamente necesarios. Un mensaje o correo puntual que acompaña un análisis
(por ejemplo, «resume los últimos 3 meses para AC032 y envíame un mensaje») va por correo,
nunca por alerta. «Envíame/mándame un mensaje» sin otro destinatario significa preparar
un correo dirigido a la persona conectada; no se envía automáticamente. Reserva alerta
para vigilar una condición futura o recurrente, especialmente «avísame cuando» o «cada día».
Si la petición mezcla una consulta y el envío puntual de sus resultados, usa correo y
conserva íntegros periodo, métrica e identificadores de filtros como AC032 en data_question.
Para una alerta, alert_metric puede ser un campo
del glosario o una fórmula/combinación de campos; conserva sus componentes, operadores
y agregaciones tal como los expresa el usuario. No conviertas la fórmula en un campo
simple ni inventes componentes. Si falta un componente o la fórmula permite resultados
materialmente distintos, pide aclaración. Extrae umbral, periodo, frecuencia y filtros
sin inventar. Hay un historial de análisis
anteriores; cuando el usuario diga 'avísame cuando' o 'sobre esto', toma métrica,
periodo y filtros del análisis pertinente más reciente. Extrae frecuencia de
comprobación de la petición actual; si falta, pide aclaración. Nunca reutilices
un umbral previo salvo que el usuario lo confirme en la petición actual.
Si falta métrica, umbral, periodo o frecuencia, needs_clarification=true.
Si el usuario cambia de tema, no heredes filtros antiguos. Marca context_used
solo si realmente has reutilizado una definición del historial y explica cuál
en context_reference. No elijas enviar correo; solo redactar.
No afirmes que hayas consultado datos ni ejecutado herramientas."""


def _fallback(message: str) -> dict:
    lower = message.lower()
    one_time_message = bool(re.search(r"\b(?:env[ií]ame|m[aá]ndame|env[ií]a|manda)\b.{0,35}\b(?:mensaje|correo|email|gmail)\b|\b(?:mensaje|correo|email|gmail)\b.{0,35}\b(?:env[ií]ame|m[aá]ndame)\b", lower))
    recurring_alert = bool(re.search(r"\b(?:cada\s+(?:d[ií]a|semana|mes)|cuando\s+(?:supere|baje|sea|llegue)|av[ií]same\s+(?:cuando|si)|notif[ií]came\s+cuando|vigila|monitoriza)\b", lower))
    route = ("alerta" if recurring_alert else
             "correo" if one_time_message else
             "alerta" if re.search(r"\b(?:alerta|aviso)\b", lower) else
             "pdf" if re.search(r"pdf|informe|documento", lower) else
             "correo" if re.search(r"correo|email|gmail|redacta", lower) else "ventas")
    return {"route": route, "data_question": message, "title": "Solicitud Oribay",
            "recipient": "", "subject": "", "alert_metric": "", "alert_operator": "",
            "alert_threshold": None, "alert_period": "", "alert_frequency": "",
            "alert_filters": "", "needs_clarification": route == "alerta",
            "context_used": False, "context_reference": "",
            "clarification": "Indica métrica, umbral, periodo y frecuencia de comprobación." if route == "alerta" else "",
            "recipient": "", "router_mode": "reglas"}


def decide(message: str, history: list | None = None) -> dict:
    token = os.environ.get("ORIBAY_TEST_ACCESS_TOKEN")
    try:
        client = Client(vertexai=True, project="aad-analitica-oee", location="global",
                        credentials=Credentials(token=token) if token else None)
        safe_history = []
        for item in (history or [])[-5:]:
            if isinstance(item, dict) and item.get("route") == "ventas":
                safe_history.append({"question": str(item.get("question", ""))[:500],
                                     "answer": str(item.get("answer", ""))[:1200]})
        response = client.models.generate_content(
            model=os.environ.get("ORIBAY_ROUTER_MODEL", "gemini-2.5-flash"),
            contents=json.dumps({"previous_sales_analyses": safe_history,
                                 "current_user_request": message}, ensure_ascii=False),
            config=types.GenerateContentConfig(system_instruction=SYSTEM, response_mime_type="application/json", temperature=0),
        )
        result = json.loads(response.text)
        if result.get("route") not in ROUTES:
            raise ValueError("Ruta desconocida")
        if result.get("route") == "alerta" and result.get("context_used") and safe_history:
            previous = safe_history[-1]["question"] + " " + safe_history[-1]["answer"]
            current_filters = result.get("alert_filters")
            filters = ([current_filters] if isinstance(current_filters, str) and current_filters.strip()
                       else current_filters if isinstance(current_filters, list) else [])
            lower_previous = previous.lower()
            lower_current = message.lower()
            if "piezas" in lower_previous and "piezas" not in lower_current and not any("piezas" in str(x).lower() for x in filters):
                filters.append("piezas")
            if "sin portes" in lower_previous and "portes" not in lower_current and not any("portes" in str(x).lower() for x in filters):
                filters.append("sin portes")
            result["alert_filters"] = filters
        result["router_mode"] = "Gemini"
        return result
    except Exception as exc:
        logging.getLogger(__name__).warning("Router Gemini no disponible: %s", exc)
        return _fallback(message)


def draft_business_email(user_request: str, verified_answer: str, suggested_subject: str = "") -> dict:
    """Turn verified sales output into a concise, editable business email."""
    try:
        token = os.environ.get("ORIBAY_TEST_ACCESS_TOKEN")
        client = Client(vertexai=True, project="aad-analitica-oee", location="global",
                        credentials=Credentials(token=token) if token else None)
        response = client.models.generate_content(
            model=os.environ.get("ORIBAY_ROUTER_MODEL", "gemini-2.5-flash"),
            contents=json.dumps({"request": user_request[:2000], "verified_sales_response": verified_answer[:10000], "subject_hint": suggested_subject[:250]}, ensure_ascii=False),
            config=types.GenerateContentConfig(
                system_instruction=("Redacta un correo comercial profesional y natural en español de España. Devuelve SOLO JSON con subject y body. "
                                    "Usa exclusivamente los hechos y cifras del texto verificado, no inventes nada, no menciones SQL ni la IA. "
                                    "Escribe las cifras con convención española: punto para millares y coma para decimales (por ejemplo, 2.083.849,69 €); "
                                    "usa € con formato claro, fechas en español y conserva sin cambios los identificadores alfanuméricos como AC032. "
                                    "No cambies el valor numérico ni recalcules cifras. "
                                    "Incluye saludo y cierre breve. Presenta las cifras con claridad; evita markdown y tablas Markdown."),
                response_mime_type="application/json", temperature=0.2),
        )
        result = json.loads(response.text)
        subject, body = str(result.get("subject", "")).strip(), str(result.get("body", "")).strip()
        if subject and body:
            return {"subject": subject[:250], "body": body[:30000]}
    except Exception as exc:
        logging.getLogger(__name__).warning("Redacción mejorada no disponible: %s", exc)
    return {"subject": suggested_subject or "Resumen comercial Oribay", "body": verified_answer[:30000]}


def draft_pdf_analysis(user_request: str, verified_answer: str, verified_tables: list | None = None) -> str:
    """Write a decision-ready Spanish analysis using only the sales agent's evidence."""
    evidence = {
        "request": user_request[:2000],
        "verified_response": verified_answer[:12000],
        "verified_tables": [str(item)[:5000] for item in (verified_tables or [])[:6]],
    }
    fallback = ("## Alcance y resultados\n\n" + verified_answer[:12000] +
                "\n\n## Lectura y límites\n\nEste informe presenta los resultados devueltos por Ventas Oribay. "
                "La respuesta disponible no incluye suficiente evidencia para atribuir causas ni validar factores externos.")
    try:
        token = os.environ.get("ORIBAY_TEST_ACCESS_TOKEN")
        client = Client(vertexai=True, project="aad-analitica-oee", location="global",
                        credentials=Credentials(token=token) if token else None)
        response = client.models.generate_content(
            model=os.environ.get("ORIBAY_ROUTER_MODEL", "gemini-2.5-flash"),
            contents=json.dumps(evidence, ensure_ascii=False),
            config=types.GenerateContentConfig(
                system_instruction=(
                    "Redacta el cuerpo de un informe comercial ejecutivo, detallado y listo para guardar, "
                    "en español natural y profesional. Devuelve Markdown limpio con títulos ## y listas breves. "
                    "Estructura: Resumen ejecutivo; Hallazgos principales; Evolución y comparativas; "
                    "Interpretación; Alcance y limitaciones. Omite secciones sin datos. Usa exclusivamente "
                    "cifras, periodos, unidades, filtros y definiciones presentes en la evidencia. No inventes "
                    "causas, objetivos, previsiones ni datos. Puedes calcular variaciones solo con cifras "
                    "comparables y explica la base del cálculo; distingue variación absoluta de porcentual. "
                    "Si faltan periodo, muestra, definiciones o contexto para concluir, dilo con claridad. "
                    "No menciones el modelo, IA, SQL ni que estás redactando. Evita repetir cifras y no añadas "
                    "conclusiones genéricas. Conserva nombres de métricas y filtros tal como aparecen en la fuente."
                ),
                temperature=0.15,
            ),
        )
        result = (response.text or "").strip()
        return result[:20000] if result else fallback
    except Exception as exc:
        logging.getLogger(__name__).warning("Análisis editorial PDF no disponible: %s", exc)
        return fallback
