import os
import dotenv

# Load the environment
dotenv.load_dotenv()

print("---- DEBUGGING ENVIRONMENT ----")
print(f"DB_HOST: {os.environ.get('DB_HOST')}")
print(f"DB_PORT: {os.environ.get('DB_PORT')}")
print(f"DB_NAME: {os.environ.get('DB_NAME')}")
print("-------------------------------")

if os.environ.get('DB_PORT') != '5433':
    print("❌ ERROR: DB_PORT is wrong! It should be 5433.")
else:
    print("✅ SUCCESS: DB_PORT is correct (5433).")