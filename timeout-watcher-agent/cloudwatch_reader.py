"""
cloudwatch_reader.py
Reads real Lambda metrics + logs from CloudWatch.

Credentials priority:
  1. Environment variables (AWS_ACCESS_KEY_ID etc.) -- Streamlit Cloud
  2. AWS named profile (devops-demo)                -- local Mac
"""

import os
import boto3
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()


def _make_session(profile="devops-demo", region="us-east-1"):
    """
    Creates boto3 session.
    Uses env vars if present (Streamlit Cloud),
    falls back to local AWS profile (your Mac).
    """
    key    = os.getenv("AWS_ACCESS_KEY_ID")
    secret = os.getenv("AWS_SECRET_ACCESS_KEY")
    token  = os.getenv("AWS_SESSION_TOKEN")  
    region = os.getenv("AWS_DEFAULT_REGION", region)

    if key and secret:
        # Streamlit Cloud -- credentials from environment variables
        return boto3.Session(
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            aws_session_token=token, 
            region_name=region,
        )
    else:
        # Local Mac -- credentials from aws configure profile
        return boto3.Session(
            profile_name=profile,
            region_name=region,
        )


class CloudWatchReader:
    def __init__(self, profile="devops-demo", region="us-east-1"):
        session      = _make_session(profile, region)
        self.cw      = session.client("cloudwatch")
        self.logs    = session.client("logs")
        self.lamb    = session.client("lambda")
        self.region  = region

    # -- Lambda config ---------------------------------------------------------
    def get_lambda_config(self, function_name):
        r = self.lamb.get_function_configuration(FunctionName=function_name)
        return {
            "timeout":       r["Timeout"],
            "memory":        r["MemorySize"],
            "runtime":       r["Runtime"],
            "last_modified": r["LastModified"],
        }

    # -- Metrics ---------------------------------------------------------------
    def get_metrics(self, function_name, minutes=30):
        end   = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes)

        def fetch(metric, stat):
            r = self.cw.get_metric_statistics(
                Namespace="AWS/Lambda",
                MetricName=metric,
                Dimensions=[{"Name": "FunctionName", "Value": function_name}],
                StartTime=start, EndTime=end,
                Period=60, Statistics=[stat],
            )
            pts = sorted(r["Datapoints"], key=lambda x: x["Timestamp"])
            return pts

        errors    = fetch("Errors",       "Sum")
        duration  = fetch("Duration",     "Maximum")
        throttles = fetch("Throttles",    "Sum")
        invokes   = fetch("Invocations",  "Sum")

        total_errors    = sum(p["Sum"]     for p in errors)
        total_invokes   = sum(p["Sum"]     for p in invokes)
        max_duration    = max((p["Maximum"] for p in duration), default=0)
        total_throttles = sum(p["Sum"]     for p in throttles)

        return {
            "total_errors":       int(total_errors),
            "total_invokes":      int(total_invokes),
            "max_duration_ms":    round(max_duration, 1),
            "total_throttles":    int(total_throttles),
            "error_rate_pct":     round((total_errors / total_invokes * 100) if total_invokes else 0, 1),
            "error_datapoints":   [(p["Timestamp"].strftime("%H:%M"), int(p["Sum"])) for p in errors],
            "duration_datapoints":[(p["Timestamp"].strftime("%H:%M"), round(p["Maximum"], 1)) for p in duration],
        }

    # -- Logs ------------------------------------------------------------------
    def get_recent_logs(self, function_name, minutes=30, max_events=20):
        log_group = f"/aws/lambda/{function_name}"
        end_ms    = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms  = end_ms - (minutes * 60 * 1000)

        try:
            streams = self.logs.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=3,
            )["logStreams"]
        except Exception:
            return []

        events = []
        for stream in streams:
            try:
                r = self.logs.get_log_events(
                    logGroupName=log_group,
                    logStreamName=stream["logStreamName"],
                    startTime=start_ms,
                    endTime=end_ms,
                    limit=max_events,
                )
                events.extend(r.get("events", []))
            except Exception:
                continue

        events.sort(key=lambda x: x["timestamp"], reverse=True)
        return [
            {
                "ts":  datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).strftime("%H:%M:%S"),
                "msg": e["message"].strip(),
            }
            for e in events[:max_events]
        ]

    # -- Detect issue ----------------------------------------------------------
    def detect_issue(self, function_name, timeout_setting):
        metrics = self.get_metrics(function_name)
        logs    = self.get_recent_logs(function_name)
        config  = self.get_lambda_config(function_name)

        issues = []

        # Scan logs for timeout message FIRST (most reliable signal)
        timeout_logs = [
            l for l in logs
            if "timed out" in l["msg"].lower() or "task timed out" in l["msg"].lower()
        ]

        # Timeout detection -- log based
        if timeout_logs:
            issues.append({
                "type":     "TIMEOUT",
                "severity": "HIGH",
                "detail":   f"Task timed out -- found {len(timeout_logs)} timeout entries. Setting: {timeout_setting}s",
            })
        # Timeout detection -- metric based (backup)
        elif metrics["max_duration_ms"] >= (timeout_setting * 1000 * 0.95):
            issues.append({
                "type":     "TIMEOUT",
                "severity": "HIGH",
                "detail":   f"Max duration {metrics['max_duration_ms']}ms hit {timeout_setting}s timeout",
            })

        # Error rate detection
        if metrics["total_errors"] > 0 and not timeout_logs:
            issues.append({
                "type":     "HIGH_ERROR_RATE",
                "severity": "HIGH",
                "detail":   f"{metrics['total_errors']} errors in last 30 min ({metrics['error_rate_pct']}%)",
            })

        # Throttle detection
        if metrics["total_throttles"] > 0:
            issues.append({
                "type":     "THROTTLING",
                "severity": "MEDIUM",
                "detail":   f"{metrics['total_throttles']} throttled invocations",
            })

        return {
            "function_name": function_name,
            "config":        config,
            "metrics":       metrics,
            "logs":          logs[:8],
            "timeout_logs":  timeout_logs[:3],
            "issues":        issues,
            "has_issue":     len(issues) > 0,
        }