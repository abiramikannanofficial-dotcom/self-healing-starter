"""
bedrock_agent.py
Sends real CloudWatch data to Claude on Bedrock.
Claude diagnoses the issue and proposes a fix.

Credentials priority:
  1. Environment variables (AWS_ACCESS_KEY_ID etc.) -- Streamlit Cloud
  2. AWS named profile (devops-demo)                -- local Mac
"""

import os
import json
import boto3
from dotenv import load_dotenv

load_dotenv()


def _make_session(profile="devops-demo", region="us-east-1"):
    key    = os.getenv("AWS_ACCESS_KEY_ID")
    secret = os.getenv("AWS_SECRET_ACCESS_KEY")
    token  = os.getenv("AWS_SESSION_TOKEN") 
    region = os.getenv("AWS_DEFAULT_REGION", region)
    
    print(f"KEY present: {bool(key)}, prefix: {key[:8] if key else 'MISSING'}")
    print(f"SECRET present: {bool(secret)}")
    print(f"TOKEN present: {bool(token)}, prefix: {token[:8] if token else 'MISSING'}")

    if key and secret:
        return boto3.Session(
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            aws_session_token=token, 
            region_name=region,
        )
    else:
        return boto3.Session(
            profile_name=profile,
            region_name=region,
        )


class BedrockAgent:
    def __init__(self, profile="devops-demo", region="us-east-1"):
        session        = _make_session(profile, region)
        self.session   = session
        self.client    = session.client("bedrock-runtime")
        self.lamb      = session.client("lambda")
        self.model_id  = "anthropic.claude-3-haiku-20240307-v1:0"

    def diagnose(self, detection: dict) -> dict:
        """Send detection data to Claude. Returns structured diagnosis + fix."""
        fn      = detection["function_name"]
        config  = detection["config"]
        metrics = detection["metrics"]
        issues  = detection["issues"]
        logs    = detection["logs"]

        prompt = f"""You are an expert AWS DevOps engineer.
A Lambda function has a problem. Analyze the data and respond ONLY with valid JSON.

## Function
- Name: {fn}
- Timeout setting: {config['timeout']}s
- Memory: {config['memory']}MB
- Runtime: {config['runtime']}

## Metrics (last 30 min)
- Total invocations: {metrics['total_invokes']}
- Total errors: {metrics['total_errors']}
- Error rate: {metrics['error_rate_pct']}%
- Max duration: {metrics['max_duration_ms']}ms
- Throttles: {metrics['total_throttles']}

## Detected issues
{json.dumps(issues, indent=2)}

## Recent log lines
{chr(10).join(f"  [{l['ts']}] {l['msg']}" for l in logs[:6])}

Respond ONLY with this JSON (no extra text, no markdown):
{{
  "root_cause": "one sentence explanation of what is wrong",
  "confidence": "HIGH or MEDIUM or LOW",
  "fix_description": "one sentence of what to change",
  "fix_action": "UPDATE_TIMEOUT or UPDATE_MEMORY or UPDATE_CONCURRENCY or NO_FIX",
  "fix_params": {{
    "timeout": <new timeout in seconds as integer, or null>,
    "memory":  <new memory in MB as integer, or null>
  }},
  "reasoning": [
    "step 1 of your reasoning",
    "step 2 of your reasoning",
    "step 3 of your reasoning"
  ],
  "risk": "LOW or MEDIUM or HIGH",
  "risk_reason": "one sentence on why this fix is safe or risky"
}}"""

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        })

        response = self.client.invoke_model(
            modelId=self.model_id,
            body=body,
            contentType="application/json",
            accept="application/json",
        )

        raw  = json.loads(response["body"].read())
        text = raw["content"][0]["text"].strip()

        # Strip markdown fences if Claude adds them
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        diagnosis = json.loads(text)
        diagnosis["input_tokens"]  = raw.get("usage", {}).get("input_tokens", 0)
        diagnosis["output_tokens"] = raw.get("usage", {}).get("output_tokens", 0)
        return diagnosis

    def apply_fix(self, function_name: str, diagnosis: dict) -> dict:
        """Apply Claude's recommended fix to the real Lambda function."""
        action  = diagnosis.get("fix_action", "NO_FIX")
        params  = diagnosis.get("fix_params", {})
        updates = {}

        if action == "UPDATE_TIMEOUT" and params.get("timeout"):
            updates["Timeout"] = int(params["timeout"])

        if action == "UPDATE_MEMORY" and params.get("memory"):
            updates["MemorySize"] = int(params["memory"])

        if not updates:
            return {"success": False, "message": "No fix to apply"}

        response = self.lamb.update_function_configuration(
            FunctionName=function_name,
            **updates,
        )

        return {
            "success":      True,
            "updated":      updates,
            "new_timeout":  response.get("Timeout"),
            "new_memory":   response.get("MemorySize"),
            "last_modified":response.get("LastModified"),
        }