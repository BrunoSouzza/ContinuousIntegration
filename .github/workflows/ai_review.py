import os, sys, json, math, textwrap, base64
from typing import List, Dict
import requests
from unidiff import PatchSet

REVIEW_FILE_EXTS = {
    ".cs", ".csproj", ".sln",
    ".js", ".jsx", ".ts", ".tsx",
    ".py",
    ".json", ".yml", ".yaml",
    ".md"
}

# ---- GitHub context ----
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
GITHUB_EVENT_PATH = os.getenv("GITHUB_EVENT_PATH")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

if not (GITHUB_REPOSITORY and GITHUB_EVENT_PATH and GITHUB_TOKEN):
    print("Missing GitHub environment variables.")
    sys.exit(1)

with open(GITHUB_EVENT_PATH, "r", encoding="utf-8") as f:
    event = json.load(f)

if "pull_request" not in event:
    print("This workflow should be triggered by pull_request.")
    sys.exit(0)

pr = event["pull_request"]
pr_number = pr["number"]

# ---- Azure OpenAI ----
AOAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AOAI_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AOAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")

if not (AOAI_ENDPOINT and AOAI_KEY and AOAI_DEPLOYMENT):
    print("Azure OpenAI secrets are missing.")
    sys.exit(1)

# Chat Completions endpoint (2024-xx api version funciona bem com gpt-4o-mini)
API_VERSION = "2024-08-01-preview"
CHAT_URL = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_DEPLOYMENT}/chat/completions?api-version={API_VERSION}"

# ---- Helpers ----
def gh_get(url: str):
    r = requests.get(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept":"application/vnd.github+json"})
    r.raise_for_status()
    return r.json()

def gh_post(url: str, payload: dict):
    r = requests.post(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept":"application/vnd.github+json"}, json=payload)
    r.raise_for_status()
    return r.json()

def should_review(filename: str) -> bool:
    fn = filename.lower()
    return any(fn.endswith(ext) for ext in REVIEW_FILE_EXTS)

def fetch_pr_files() -> List[Dict]:
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}/files?per_page=100"
    files = gh_get(url)
    # você pode paginar se necessário (para PRs gigantes)
    return [f for f in files if should_review(f.get("filename","")) and f.get("status") != "removed"]

def fetch_file_content(sha: str) -> str:
    # baixa conteúdo bruto pelo blob SHA
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/git/blobs/{sha}"
    data = gh_get(url)
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return data.get("content","")

def split_chunks(text: str, max_chars: int = 7000) -> List[str]:
    # corte simples por tamanho para evitar janelas muito grandes
    if len(text) <= max_chars:
        return [text]
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i+max_chars])
        i += max_chars
    return chunks

SYSTEM_PROMPT = """Você é um revisor de código sênior. Avalie alterações em PRs com foco em:
- Bugs potenciais, regressões, condições de corrida;
- Segurança (injeções, secrets, validação, authZ/authN);
- Qualidade (nomenclatura, complexidade, duplicação, SOLID/Clean Code);
- Performance (alocação, I/O, queries, async/await, paralelismo);
- Testabilidade (cobertura, casos faltantes, mocks);
- Estilo e consistência (linters, convenções do projeto).

Responda sucintamente, mas com exemplos concretos (trechos de código em blocos).
Quando possível, sugira diffs com ```suggestion``` para facilitar o apply no GitHub.
"""

def call_aoai(messages: List[Dict], temperature: float = 0.2) -> str:
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 1200
    }
    r = requests.post(
        CHAT_URL,
        headers={"api-key": AOAI_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=90
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

def build_file_prompt(filename: str, patch: str, content: str) -> str:
    # Mostre diff e trechos relevantes do arquivo
    # (evita mandar arquivo inteiro para modelos pequenos)
    preview = content[:4000]  # prevenção simples
    return textwrap.dedent(f"""
    Arquivo: {filename}

    DIFF (unified):
    ```
    {patch[:6000]}
    ```

    Trecho do conteúdo atual (início):
    ```
    {preview}
    ```

    Tarefa:
    - Aponte problemas específicos por tópicos.
    - Dê sugestões práticas e, quando aplicável, use blocos ```suggestion``` com o trecho corrigido.
    - Se o diff estiver ok, diga explicitamente que não encontrou nada crítico.
    """)

def post_review_comment(markdown_body: str):
    # Publica um único review consolidado
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}/reviews"
    payload = {"body": markdown_body, "event": "COMMENT"}
    gh_post(url, payload)

def get_diff_for_file(file_json: Dict) -> str:
    # utiliza o patch fornecido pela própria API de PR files
    return file_json.get("patch", "")

# ---- Execução ----
files = fetch_pr_files()
if not files:
    post_review_comment("🤖 Nenhum arquivo relevante para revisão automática (extensões filtradas).")
    sys.exit(0)

all_sections = []
for f in files:
    filename = f["filename"]
    patch = get_diff_for_file(f) or ""
    # pular arquivos sem patch (binários ou renomeações sem alteração)
    if not patch.strip():
        continue

    # baixa conteúdo do arquivo no HEAD da PR
    head_sha = f.get("sha")
    content = ""
    if head_sha:
        try:
            content = fetch_file_content(head_sha)
        except Exception as e:
            content = ""
    
    prompt = build_file_prompt(filename, patch, content)
    chunks = split_chunks(prompt, max_chars=7000)

    file_feedback_parts = []
    for i, chunk in enumerate(chunks, start=1):
        messages = [
            {"role":"system", "content": SYSTEM_PROMPT},
            {"role":"user", "content": chunk}
        ]
        try:
            ans = call_aoai(messages)
            file_feedback_parts.append(ans.strip())
        except Exception as e:
            file_feedback_parts.append(f"Falha ao analisar este bloco ({i}): {e}")

    section = f"### `{filename}`\n" + "\n\n".join(file_feedback_parts)
    all_sections.append(section)

if not all_sections:
    body = "🤖 Consegui ler os arquivos, mas não havia *diff* textual analisável."
else:
    body = (
        "## 🤖 Azure OpenAI Code Review\n"
        f"- PR: #{pr_number}\n"
        "- Escopo: arquivos filtrados por extensão e *diff* textual.\n\n"
        + "\n\n---\n\n".join(all_sections)
    )

post_review_comment(body)
print("AI review posted.")
