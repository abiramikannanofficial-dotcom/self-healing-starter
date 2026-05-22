# Agentic DevOps — Self-Healing Cloud Infrastructure

AI agent that detects Lambda failures, diagnoses via Claude on Bedrock,
and self-heals with human approval.

## Stack
- Streamlit — live dashboard
- AWS CloudWatch — real metrics
- AWS Bedrock (Claude) — AI diagnosis
- AWS Lambda — target function

## Run locally
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in your AWS credentials in .env
streamlit run dashboard.py
```
