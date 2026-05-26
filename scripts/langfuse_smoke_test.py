import os
from dotenv import load_dotenv

load_dotenv()

# Langfuse host env var doit s'appeler LANGFUSE_HOST (pas BASE_URL)
os.environ.setdefault("LANGFUSE_HOST", os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"))

from langfuse.openai import AzureOpenAI

client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_API_KEY"],
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_version=os.environ["AZURE_OPENAI_API_VERSION"],
)

response = client.chat.completions.create(
    model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
    messages=[{"role": "user", "content": "Dis bonjour en 5 mots."}],
    name="smoke-test",
)

print(response.choices[0].message.content)
