#!/bin/bash
# Deploys demo-healing-function to AWS
# Usage: ./lambda/deploy.sh

FUNCTION_NAME="demo-healing-function"
REGION="us-east-1"
PROFILE="devops-demo"
ROLE_ARN="arn:aws:iam::877969058937:role/lambda-demo-role"

echo "Zipping function..."
cd lambda
zip function.zip lambda_function.py

echo "Deploying to AWS..."
aws lambda update-function-code \
  --function-name $FUNCTION_NAME \
  --zip-file fileb://function.zip \
  --region $REGION \
  --profile $PROFILE

echo "Done! Function deployed."
rm function.zip
cd ..
EOF

