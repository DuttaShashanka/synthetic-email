import os
import hashlib
import html
from dataclasses import asdict
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

from src.models import TransformContext
from src.entity_graph import EntityGraph
from src.parsing import parse_email
from src.pipeline import apply_replacement_edits, synthesize
from src.privacy import redact_direct_identifiers, redact_source_terms, source_terms


load_dotenv()

DEFAULT_MODEL_OPTIONS = (
    "meta-llama/llama-3.1-8b-instruct",
    "openai/gpt-4o-mini",
    "google/gemini-2.0-flash-001",
)


@st.cache_data(ttl=3600, show_spinner=False)
def get_openrouter_models() -> tuple[tuple[str, str], ...]:
    try:
        response = requests.get("https://openrouter.ai/api/v1/models", timeout=8)
        response.raise_for_status()
        entries = response.json().get("data", [])
        models = {
            entry["id"]: entry.get("name") or entry["id"]
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }
        if models:
            return tuple(sorted(models.items(), key=lambda item: item[1].casefold()))
    except (requests.RequestException, ValueError, TypeError):
        pass

    return tuple((model_id, model_id) for model_id in DEFAULT_MODEL_OPTIONS)

st.set_page_config(
    page_title="Synthetic Email Workbench",
    page_icon="✉",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root {
        --ink: #17231f;
        --muted: #65736d;
        --line: #dce5df;
        --paper: #f5f7f3;
        --green: #1c654d;
        --lime: #d9eb8f;
    }
    .stApp { background: var(--paper); color: var(--ink); }
    [data-testid="stSidebar"] { background: #eaf0e9; border-right: 1px solid var(--line); }
    h1, h2, h3 { color: var(--ink); letter-spacing: 0; }
    h1 { font-family: Georgia, "Times New Roman", serif; font-weight: 500; }
    label, [data-testid="stWidgetLabel"], [data-testid="stWidgetLabel"] p,
    [data-testid="stRadio"] label, [data-testid="stRadio"] label p,
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"],
    [data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {
        color: var(--ink) !important;
    }
    [data-testid="stTextInput"] input,
    [data-testid="stNumberInput"] input,
    [data-testid="stTextArea"] textarea {
        background: #ffffff !important;
        color: var(--ink) !important;
        -webkit-text-fill-color: var(--ink) !important;
        caret-color: var(--ink);
        opacity: 1 !important;
    }
    .eyebrow {
        color: var(--green); font-size: 0.72rem; font-weight: 700;
        letter-spacing: 0.12em; text-transform: uppercase;
    }
    .stage {
        min-height: 86px; padding: 13px 15px; border: 1px solid var(--line);
        border-top: 3px solid var(--green); background: #fbfcfa;
    }
    .stage strong { display: block; margin-bottom: 5px; }
    .stage span { color: var(--muted); font-size: 0.82rem; }
    .replacement-source, .replacement-value {
        padding: 9px 11px; border-radius: 4px; min-height: 42px;
        overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .replacement-source { background: #fff0d6; border-left: 3px solid #c47b16; color: #4a3211; }
    .replacement-value { background: #e2f1e8; border-left: 3px solid var(--green); color: var(--ink); }
    [data-testid="stMetric"] {
        border: 1px solid var(--line); background: #fbfcfa; padding: 12px 14px;
    }
    [data-testid="stMetricLabel"], [data-testid="stMetricLabel"] *,
    [data-testid="stMetricValue"], [data-testid="stMetricValue"] * {
        color: var(--ink) !important;
        -webkit-text-fill-color: var(--ink) !important;
        opacity: 1 !important;
    }
    div[data-testid="stTabs"] [role="tab"],
    div[data-testid="stTabs"] [role="tab"] * {
        color: var(--ink) !important;
    }
    div[data-testid="stTabs"] [role="tab"][aria-selected="true"],
    div[data-testid="stTabs"] [role="tab"][aria-selected="true"] * {
        color: var(--green) !important;
    }
    .stButton > button[kind="primary"] {
        background: var(--green); border-color: var(--green); color: #ffffff;
    }
    [data-testid="stDownloadButton"] button,
    [data-testid="stDownloadButton"] button * {
        background: var(--green) !important;
        border-color: var(--green) !important;
        color: #ffffff !important;
        -webkit-text-fill-color: #ffffff !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


SAMPLE_EMAIL = Path(__file__).parent / "examples" / "input_email.txt"

with st.sidebar:
    st.markdown('<p class="eyebrow">Run configuration</p>', unsafe_allow_html=True)
    mode = st.radio("Rewrite mode", ["OpenRouter"], index=0)
    industry = st.text_input("Target industry", value="agricultural technology")
    max_attempts = st.number_input("Maximum validation attempts", min_value=1, max_value=5, value=3)
    configured_model = os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL_OPTIONS[0])
    if mode == "OpenRouter":
        model_catalogue = dict(get_openrouter_models())
        if configured_model not in model_catalogue:
            model_catalogue[configured_model] = f"Configured: {configured_model}"
        model_ids = list(model_catalogue)
        model = st.selectbox(
            "OpenRouter model",
            options=model_ids,
            index=model_ids.index(configured_model),
            format_func=lambda model_id: f"{model_catalogue[model_id]} ({model_id})",
            help="Search model names or IDs from the OpenRouter catalogue.",
        )
    else:
        model = configured_model
    key_ready = bool(os.getenv("OPENROUTER_API_KEY"))
    st.caption("OpenRouter credential: configured" if key_ready else "OpenRouter credential: not configured")
    if mode == "OpenRouter":
        st.info("Only the directly sanitized email is sent to OpenRouter. Avoid entering confidential data unless approved for external processing.")

st.markdown('<p class="eyebrow">Privacy-first email transformation</p>', unsafe_allow_html=True)
st.title("Synthetic Email Workbench")
st.write("Inspect each stage of a source email transformation and its disclosure-risk checks.")

source_default = SAMPLE_EMAIL.read_text(encoding="utf-8") if SAMPLE_EMAIL.exists() else ""
source = st.text_area("Source email", value=source_default, height=250, label_visibility="visible")
run_clicked = st.button("Run transformation", type="primary", use_container_width=False)

if run_clicked:
    st.session_state.pop("transformation", None)
    if not source.strip():
        st.error("Enter an email before running the transformation.")
    else:
        parsed = parse_email(source)
        deny_terms = source_terms(source)
        sanitized = redact_direct_identifiers(source, TransformContext())
        sanitized = redact_source_terms(sanitized, deny_terms)
        transformation = {
            "source": source,
            "mode": mode,
            "parsed": parsed,
            "sanitized": sanitized,
            "candidate": None,
            "report": None,
            "attempts": 0,
            "error": None,
            "replacements": [],
        }
        try:
            replacement_records = []
            candidate, report, pipeline_sanitized, attempts = synthesize(
                source,
                industry.strip() or "general business",
                model.strip() or None,
                offline=mode == "Offline",
                max_attempts=int(max_attempts),
                replacement_sink=replacement_records,
            )
            transformation.update(
                sanitized=pipeline_sanitized,
                candidate=candidate,
                report=report,
                attempts=attempts,
                replacements=replacement_records,
            )
        except Exception as error:
            transformation["error"] = str(error)
            transformation["replacements"] = replacement_records
        st.session_state["transformation"] = transformation

result = st.session_state.get("transformation")
if result and result["source"] != source:
    st.info("The source email changed. Run the transformation again to refresh these results.")
    result = None

if result:
    parsed = result["parsed"]
    report = result["report"]
    st.markdown("### Pipeline")
    stages = [
        ("01  Parse", "Headers and body separated"),
        ("02  Sanitize", "Direct identifiers replaced"),
        ("03  Rewrite", result["mode"]),
        ("04  Validate", "Accepted" if report and report.valid else "Blocked"),
    ]
    columns = st.columns(4)
    for column, (title, detail) in zip(columns, stages):
        column.markdown(f'<div class="stage"><strong>{title}</strong><span>{detail}</span></div>', unsafe_allow_html=True)

    if result["error"]:
        st.error(f"Transformation failed closed: {result['error']}")

    parse_tab, sanitized_tab, replacements_tab, output_tab, validation_tab = st.tabs(
        ["Parsed email", "Sanitized input", "Replacements", "Synthetic output", "Validation"]
    )
    with parse_tab:
        header_columns = st.columns(4)
        header_values = [
            ("Sender", parsed.sender or "Not found"),
            ("Recipients", ", ".join(parsed.recipients) or "Not found"),
            ("Date", parsed.date or "Not found"),
            ("Subject", parsed.subject or "Not found"),
        ]
        for column, (label, value) in zip(header_columns, header_values):
            column.metric(label, value)
        st.text_area("Parsed body", value=parsed.body, height=180, disabled=True)

    with sanitized_tab:
        st.caption("This is the only source-derived content eligible for an OpenRouter request.")
        st.text_area("Pre-generation sanitized email", value=result["sanitized"], height=360, disabled=True)

    with replacements_tab:
        replacements = result.get("replacements", [])
        if not replacements:
            st.info("No direct-identifier replacements were captured for this email.")
        else:
            st.caption("Review the detected mappings. Saving applies edits to this output and persists them in the local entity graph.")
            with st.form("replacement_review_form"):
                edited_values = {}
                for index, record in enumerate(replacements):
                    source_value = html.escape(record["source_value"])
                    st.markdown(
                        f'<div class="replacement-source"><strong>{html.escape(record["entity_type"])}</strong><br>{source_value}</div>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div class="replacement-value">{html.escape(record["replacement"])}</div>',
                        unsafe_allow_html=True,
                    )
                    key_material = (
                        f'{record["entity_type"]}:{record["source_value"]}:{record["replacement"]}'
                    ).encode("utf-8")
                    widget_key = "replacement_" + hashlib.sha256(key_material).hexdigest()[:16]
                    edited_values[index] = st.text_input(
                        "Synthetic replacement",
                        value=record["replacement"],
                        key=widget_key,
                        label_visibility="collapsed",
                    )
                save_clicked = st.form_submit_button("Save replacements", type="primary")

            if save_clicked:
                try:
                    updated_sanitized, updated_candidate, updated_records, updated_report = apply_replacement_edits(
                        result["source"],
                        result["sanitized"],
                        result["candidate"],
                        replacements,
                        edited_values,
                        EntityGraph(),
                    )
                    st.session_state["transformation"].update(
                        sanitized=updated_sanitized,
                        candidate=updated_candidate,
                        replacements=updated_records,
                        report=updated_report,
                    )
                    if updated_candidate is None:
                        st.success("Replacement edits were validated and saved to the knowledge graph. This run has no accepted synthetic output.")
                    else:
                        st.success("Replacement edits were validated and saved to the knowledge graph.")
                except ValueError as error:
                    st.error(str(error))

    with output_tab:
        if result["candidate"]:
            st.text_area("Accepted synthetic email", value=result["candidate"], height=360, disabled=True)
            st.download_button(
                "Download synthetic email",
                data=result["candidate"],
                file_name="synthetic_email.txt",
                mime="text/plain",
                type="primary",
                use_container_width=False,
            )
        else:
            st.info("No synthetic output was accepted, so no candidate is shown.")

    with validation_tab:
        if report:
            metric_columns = st.columns(3)
            metric_columns[0].metric("Result", "PASS" if report.valid else "BLOCKED")
            metric_columns[1].metric("Risk score", f"{report.risk_score:.3f}")
            metric_columns[2].metric("Attempts", result["attempts"])
            st.progress(max(0.0, min(1.0, report.risk_score)), text="Disclosure risk")
            st.json(asdict(report))
        else:
            st.warning("Validation did not produce an accepted candidate. The pipeline stopped without returning unsafe output.")
else:
    st.markdown("### Pipeline")
    columns = st.columns(4)
    for column, (title, detail) in zip(
        columns,
        [
            ("01  Parse", "Email headers and body"),
            ("02  Sanitize", "Deterministic substitutions"),
            ("03  Rewrite", "Offline or OpenRouter"),
            ("04  Validate", "Privacy and structure gates"),
        ],
    ):
        column.markdown(f'<div class="stage"><strong>{title}</strong><span>{detail}</span></div>', unsafe_allow_html=True)