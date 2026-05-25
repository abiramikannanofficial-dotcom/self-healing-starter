"""
dashboard.py — Agentic DevOps · Real-Time Self-Healing
Detects real Lambda issues via CloudWatch,
diagnoses via Claude on Bedrock,
asks for human approval before opening a GitHub PR.
Merging the PR applies the fix via GitHub Actions.

Run: streamlit run dashboard.py
"""

import time
import json
import threading
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv
from pr_agent import open_fix_pr

load_dotenv()

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Agentic DevOps",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .stApp { background-color: #0d1117; color: #e6edf3; }
    div[data-testid="metric-container"] {
        background:#161b22; border:1px solid #30363d; border-radius:8px; padding:12px 16px;
    }
    .log-box {
        background:#010409; border:1px solid #30363d; border-radius:8px; padding:14px;
        font-family:'Courier New',monospace; font-size:12px;
        max-height:220px; overflow-y:auto;
    }
    .diagnosis-box {
        background:#161b22; border:1px solid #1f6feb; border-radius:10px; padding:18px; margin:8px 0;
    }
    .fix-box {
        background:#0d1f12; border:1px solid #238636; border-radius:10px; padding:18px; margin:8px 0;
    }
    .risk-low    { color:#3fb950; font-weight:600; }
    .risk-medium { color:#e3b341; font-weight:600; }
    .risk-high   { color:#f85149; font-weight:600; }
    .badge-ok    { background:#1a4731;color:#3fb950;border-radius:20px;padding:3px 12px;font-size:12px; }
    .badge-alarm { background:#3d1f24;color:#f85149;border-radius:20px;padding:3px 12px;font-size:12px; }
    .badge-busy  { background:#1c2d4a;color:#58a6ff;border-radius:20px;padding:3px 12px;font-size:12px; }
    .badge-done  { background:#1a4731;color:#3fb950;border-radius:20px;padding:3px 12px;font-size:12px; }
    .stage-done    { background:#1a4731;color:#3fb950;border:1px solid #238636;border-radius:6px;padding:4px 10px;font-size:11px;text-align:center; }
    .stage-active  { background:#1c2d4a;color:#58a6ff;border:1px solid #1f6feb;border-radius:6px;padding:4px 10px;font-size:11px;text-align:center; }
    .stage-pending { background:#161b22;color:#6e7681;border:1px solid #30363d;border-radius:6px;padding:4px 10px;font-size:11px;text-align:center; }
    div[data-testid="column"]:nth-of-type(3) button {
        background-color:#1f6feb!important; color:#ffffff!important;
        border:1px solid #388bfd!important; font-weight:600;
    }
    div[data-testid="column"]:nth-of-type(3) button:hover {
        background-color:#388bfd!important; color:#ffffff!important;
    }
    div[data-testid="column"]:nth-of-type(3) button:disabled {
        background-color:#1c2d4a!important; color:#58a6ff!important;
        border:1px solid #1f6feb!important; opacity:0.6;
    }
    #MainMenu,footer,header { visibility:hidden; }
    .block-container { padding-top:1.2rem; }
    hr { border-color:#21262d; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
DEFAULTS = {
    "agent_stage":    -1,
    "agent_status":   "idle",
    "agent_logs":     ["Waiting — press ⚡ Detect Issue to start."],
    "event_logs":     [],
    "detection":      None,
    "diagnosis":      None,
    "pr_result":      None,
    "approval_state": None,
    "running":        False,
    "function_name":  "demo-healing-function",
    "profile":        "devops-demo",
    "region":         "us-east-1",
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Shared dict (thread-safe) ─────────────────────────────────────────────────
if "SH" not in st.session_state:
    st.session_state.SH = {
        "agent_stage":    -1,
        "agent_status":   "idle",
        "agent_logs":     ["Waiting — press ⚡ Detect Issue to start."],
        "event_logs":     [],
        "detection":      None,
        "diagnosis":      None,
        "pr_result":      None,
        "approval_state": None,
        "running":        False,
    }
SH = st.session_state.SH

# ── Helpers ───────────────────────────────────────────────────────────────────
STAGES = ["Detect", "Diagnose", "Awaiting Approval", "Open PR", "Verify"]
ICONS  = ["🔍",     "🧠",       "🙋",                "🔀",      "✅"]

def ts():
    return datetime.now().strftime("%H:%M:%S")

def sh_log(msg):
    SH["agent_logs"].append(f"[{ts()}] {msg}")
    SH["agent_logs"] = SH["agent_logs"][-50:]

def sh_event(msg):
    SH["event_logs"].insert(0, f"[{ts()}] {msg}")
    SH["event_logs"] = SH["event_logs"][:30]

def render_pipeline():
    stage    = SH["agent_stage"]
    all_done = stage == 99
    cols     = st.columns(len(STAGES) * 2 - 1)
    for i, (icon, label) in enumerate(zip(ICONS, STAGES)):
        col = cols[i * 2]
        if all_done or i < stage:
            css = "stage-done"
        elif i == stage:
            css = "stage-active"
        else:
            css = "stage-pending"
        col.markdown(f'<div class="{css}">{icon} {label}</div>', unsafe_allow_html=True)
        if i < len(STAGES) - 1:
            cols[i * 2 + 1].markdown(
                '<div style="text-align:center;color:#6e7681;padding-top:3px">──▶</div>',
                unsafe_allow_html=True,
            )

def render_log_box(lines):
    content = "\n".join(lines[-20:])
    st.markdown(
        f'<div class="log-box"><pre style="margin:0;color:#e6edf3;white-space:pre-wrap">{content}</pre></div>',
        unsafe_allow_html=True,
    )

# ── Cached resources ──────────────────────────────────────────────────────────
def get_reader(profile, region):
    from cloudwatch_reader import CloudWatchReader
    return CloudWatchReader(profile=profile, region=region)

@st.cache_resource
def get_agent(profile, region):
    from bedrock_agent import BedrockAgent
    return BedrockAgent(profile=profile, region=region)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Settings")
    st.markdown("---")
    fn      = st.text_input("Lambda function name", value=st.session_state.function_name)
    profile = st.text_input("AWS profile",          value=st.session_state.profile)
    region  = st.text_input("AWS region",           value=st.session_state.region)
    st.session_state.function_name = fn
    st.session_state.profile       = profile
    st.session_state.region        = region
    st.markdown("---")
    st.caption("Detect → Diagnose → Approve → PR → Merge → Fix")

# ── Header ────────────────────────────────────────────────────────────────────
h1, h2, h3, h4 = st.columns([3.5, 2, 1.5, 1])

with h1:
    st.markdown("## 🤖 Agentic DevOps")
    st.caption(f"Monitoring: `{fn}` · {region}")

with h2:
    st.markdown("<br>", unsafe_allow_html=True)
    badge_map = {
        "idle":  ("badge-ok",    "● Idle"),
        "busy":  ("badge-busy",  "◉ Running"),
        "alert": ("badge-alarm", "⚠ Issue detected"),
        "done":  ("badge-done",  "✅ PR Opened"),
    }
    css, lbl = badge_map.get(SH["agent_status"], ("badge-ok", "● Idle"))
    st.markdown(f'<span class="{css}">{lbl}</span>', unsafe_allow_html=True)

with h3:
    st.markdown("<br>", unsafe_allow_html=True)
    detect_btn = st.button(
        "⚡ Detect Issue",
        disabled=SH["running"] or SH["approval_state"] == "pending",
        use_container_width=True,
    )

with h4:
    st.markdown("<br>", unsafe_allow_html=True)
    reset_btn = st.button("↺ Reset", use_container_width=True)

st.markdown("---")

# ── Pipeline ──────────────────────────────────────────────────────────────────
st.markdown("**Agent Pipeline**")
render_pipeline()
st.markdown("---")

# ── Metrics row ───────────────────────────────────────────────────────────────
det = SH.get("detection")
m1, m2, m3, m4 = st.columns(4)
if det:
    cfg = det["config"]
    mtr = det["metrics"]
    m1.metric("Timeout setting",  f"{cfg['timeout']}s")
    m2.metric("Max duration",     f"{mtr['max_duration_ms']}ms")
    m3.metric("Errors (30 min)",  mtr["total_errors"])
    m4.metric("Error rate",       f"{mtr['error_rate_pct']}%")
else:
    m1.metric("Timeout setting",  "—")
    m2.metric("Max duration",     "—")
    m3.metric("Errors (30 min)",  "—")
    m4.metric("Error rate",       "—")

st.markdown("---")

# ── Agent log + Event log ─────────────────────────────────────────────────────
col_log, col_events = st.columns(2)

with col_log:
    st.markdown("**Agent Reasoning Stream**")
    render_log_box(SH["agent_logs"])

with col_events:
    st.markdown("**CloudWatch Event Log**")
    if SH["event_logs"]:
        render_log_box(SH["event_logs"])
    else:
        st.caption("No events yet.")

st.markdown("---")

# ── Diagnosis panel ───────────────────────────────────────────────────────────
diag = SH.get("diagnosis")
if diag:
    st.markdown("### Claude's Diagnosis")
    col_diag, col_fix = st.columns(2)

    with col_diag:
        st.markdown('<div class="diagnosis-box">', unsafe_allow_html=True)
        st.markdown("**Root cause**")
        st.markdown(f"> {diag.get('root_cause','—')}")
        st.markdown(f"**Confidence:** `{diag.get('confidence','—')}`")
        st.markdown("**Reasoning:**")
        for step in diag.get("reasoning", []):
            st.markdown(f"- {step}")
        st.markdown('</div>', unsafe_allow_html=True)

    with col_fix:
        st.markdown('<div class="fix-box">', unsafe_allow_html=True)
        st.markdown("**Proposed fix**")
        st.markdown(f"> {diag.get('fix_description','—')}")
        params = diag.get("fix_params", {})
        if params.get("timeout"):
            st.markdown(f"- Timeout: `{det['config']['timeout']}s` → `{params['timeout']}s`")
        if params.get("memory"):
            st.markdown(f"- Memory: `{det['config']['memory']}MB` → `{params['memory']}MB`")
        risk     = diag.get("risk", "LOW")
        risk_css = {"LOW":"risk-low","MEDIUM":"risk-medium","HIGH":"risk-high"}.get(risk,"risk-low")
        st.markdown(f'**Risk:** <span class="{risk_css}">{risk}</span>', unsafe_allow_html=True)
        st.markdown(f"_{diag.get('risk_reason','')}_")
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("---")

    # ── Human approval ────────────────────────────────────────────────────────
    approval = SH.get("approval_state")

    if approval == "pending":
        st.markdown("### Human Approval Required")
        st.info(
            f"Claude wants to: **{diag.get('fix_description')}**  \n"
            f"Approving will open a GitHub PR. The fix is applied only when the PR is merged."
        )

        col_approve, col_reject, col_space = st.columns([1, 1, 3])
        with col_approve:
            if st.button("Approve & Open PR", use_container_width=True):
                SH["approval_state"] = "approved"
                st.rerun()
        with col_reject:
            if st.button("Reject", use_container_width=True):
                SH["approval_state"] = "rejected"
                sh_log("Fix rejected by human. No PR opened.")
                sh_event("Fix REJECTED by operator. Lambda unchanged.")
                SH["agent_stage"]  = 99
                SH["agent_status"] = "idle"
                st.rerun()

    elif approval == "rejected":
        st.error("Fix rejected. No changes were made to Lambda.")

    elif approval == "approved":
        pr_result = SH.get("pr_result")

        if pr_result is None:
            # Open PR now
            with st.spinner("Creating branch and opening PR on GitHub…"):
                result = open_fix_pr(
                    detection  = SH["detection"],
                    diagnosis  = SH["diagnosis"],
                )
                SH["pr_result"]    = result
                SH["agent_stage"]  = 99
                SH["agent_status"] = "done"
                if result["success"]:
                    sh_log(f"🔀 PR opened: {result['pr_url']}")
                    sh_event(f"PR created: {result['branch']}")
                else:
                    sh_log(f"PR failed: {result['message']}")
                    sh_event(f"PR error: {result['message']}")
            st.rerun()

        elif pr_result.get("success"):
            st.success("PR opened successfully!")
            st.markdown(
                f"🔀 **[View PR on GitHub]({pr_result['pr_url']})**  \n"
                f"Branch: `{pr_result['branch']}`  \n"
                f"Merge the PR to apply the fix to Lambda automatically via GitHub Actions."
            )
        else:
            st.error(f"PR failed: {pr_result['message']}")
            if st.button("Retry PR"):
                SH["pr_result"] = None
                st.rerun()

# ── Detection raw data ────────────────────────────────────────────────────────
if det and det.get("logs"):
    with st.expander("📋 Raw CloudWatch Logs", expanded=False):
        for l in det["logs"]:
            st.text(f"[{l['ts']}] {l['msg']}")

# ── Agent thread ──────────────────────────────────────────────────────────────
def detect_and_diagnose(fn, profile, region):
    try:
        # ── Stage 0 — Invoke Lambda to trigger failure ────────────────────────
        SH["agent_stage"]  = 0
        SH["agent_status"] = "busy"
        sh_log("⚡ Invoking Lambda to trigger failure…")
        sh_event(f"Invoking Lambda: {fn}")

        reader = get_reader(profile, region)
        sh_log("Got the reader")

        cfg_live     = reader.get_lambda_config(fn)
        real_timeout = cfg_live["timeout"]
        sh_log(f"🔍 Lambda config — timeout: {real_timeout}s  memory: {cfg_live['memory']}MB")

        try:
            resp   = reader.lamb.invoke(
                FunctionName=fn,
                InvocationType="RequestResponse",
                Payload=json.dumps({"test": "trigger"}).encode(),
            )
            result = json.loads(resp["Payload"].read())
            if resp.get("FunctionError"):
                sh_log(f"⚡ Lambda errored as expected: {result.get('errorType','unknown')}")
                sh_event(f"Lambda error triggered: {result.get('errorType')}")
            else:
                sh_log("Lambda completed without error")
                sh_event("Lambda invoked — no error triggered")
        except Exception as e:
            sh_log(f"Invoke exception: {str(e)[:80]}")

        # ── Wait for CloudWatch logs ──────────────────────────────────────────
        sh_log("⏳ Polling CloudWatch — waiting for data to appear…")
        max_attempts = 6
        for attempt in range(1, max_attempts + 1):
            time.sleep(5)
            sh_log(f"⏳ Checking… attempt {attempt}/{max_attempts} ({attempt * 5}s elapsed)")
            check     = reader.detect_issue(fn, timeout_setting=real_timeout)
            tl        = check.get("timeout_logs", [])
            has_issue = check.get("has_issue", False)
            if tl or has_issue:
                sh_log("✅ Issue data found in CloudWatch.")
                break
        else:
            sh_log("⚠ Proceeding with available data")

        # ── Detect issues ─────────────────────────────────────────────────────
        sh_log("🔍 Analysing CloudWatch metrics and logs…")
        detection       = reader.detect_issue(fn, timeout_setting=real_timeout)
        SH["detection"] = detection

        mtr = detection["metrics"]
        sh_log(f"🔍 Invocations: {mtr['total_invokes']}  Errors: {mtr['total_errors']}")
        sh_log(f"🔍 Max duration: {mtr['max_duration_ms']}ms  Throttles: {mtr['total_throttles']}")

        if not detection["has_issue"]:
            sh_log("🔍 No issues detected. Function looks healthy.")
            sh_event("No issues found.")
            SH["agent_status"] = "idle"
            SH["agent_stage"]  = 99
            SH["running"]      = False
            return

        for issue in detection["issues"]:
            sh_log(f"⚠ Issue: [{issue['severity']}] {issue['detail']}")
            sh_event(f"ISSUE: {issue['type']} — {issue['detail']}")

        SH["agent_status"] = "alert"
        time.sleep(0.5)

        # ── Stage 1 — Diagnose with Claude ────────────────────────────────────
        SH["agent_stage"] = 1
        sh_log("🧠 Sending data to Claude on Bedrock…")
        sh_event("Claude analysis started")

        agent     = get_agent(profile, region)
        diagnosis = agent.diagnose(detection)
        SH["diagnosis"] = diagnosis

        sh_log(f"Root cause: {diagnosis.get('root_cause')}")
        sh_log(f"Confidence: {diagnosis.get('confidence')}")
        sh_log(f"Fix: {diagnosis.get('fix_description')}")
        sh_log(f"Risk: {diagnosis.get('risk')}")
        for step in diagnosis.get("reasoning", []):
            sh_log(f"   → {step}")
        sh_event(f"Claude diagnosis: {diagnosis.get('fix_action')}")
        time.sleep(0.5)

        # ── Stage 2 — Await human approval ────────────────────────────────────
        SH["agent_stage"]    = 2
        SH["approval_state"] = "pending"
        sh_log("🙋 Waiting for human approval…")
        sh_event("Awaiting operator approval before opening PR")

        # Poll every 0.5s, max 5 minutes
        for _ in range(600):
            time.sleep(0.5)
            if SH["approval_state"] == "approved":
                break
            if SH["approval_state"] == "rejected":
                SH["running"] = False
                return

        if SH["approval_state"] != "approved":
            sh_log("⏰ Approval timed out. No changes made.")
            SH["running"] = False
            return

        # ── Stage 3 — PR opened by dashboard UI (not thread) ─────────────────
        SH["agent_stage"] = 3
        sh_log("🔀 Approval received — opening PR on GitHub…")
        sh_event("PR creation triggered by operator approval")

    except Exception as e:
        sh_log(f"Error: {str(e)}")
        sh_event(f"Agent error: {str(e)[:60]}")
        SH["agent_status"] = "idle"
    finally:
        SH["running"] = False


# ── Button handlers ───────────────────────────────────────────────────────────
if reset_btn:
    for k, v in DEFAULTS.items():
        st.session_state[k] = v
    st.session_state.SH = {
        "agent_stage":    -1,
        "agent_status":   "idle",
        "agent_logs":     ["Reset. Press ⚡ Detect Issue to start."],
        "event_logs":     [],
        "detection":      None,
        "diagnosis":      None,
        "pr_result":      None,
        "approval_state": None,
        "running":        False,
    }
    st.rerun()

if detect_btn and not SH["running"]:
    SH["running"] = True
    threading.Thread(
        target=detect_and_diagnose,
        args=(
            st.session_state.function_name,
            st.session_state.profile,
            st.session_state.region,
        ),
        daemon=True,
    ).start()

# ── Auto-rerun ────────────────────────────────────────────────────────────────
if SH["running"] or SH["approval_state"] == "pending":
    time.sleep(0.5)
    st.rerun()