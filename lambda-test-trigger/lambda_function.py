import time
import json

def lambda_handler(event, context):
    print("Function started...")
    print(f"Memory limit: {context.memory_limit_in_mb}MB")
    print(f"Time remaining: {context.get_remaining_time_in_millis()}ms")
    
    # Intentionally slow — will exceed the configured timeout
    time.sleep(6)
    
    print("Done!")  # never reaches here
    return {"status": "ok"}
