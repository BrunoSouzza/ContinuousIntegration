import os, sys, json, math, textwrap, base64, re
from typing import List, Dict, Optional, Tuple
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

# Chat Completions endpoint
API_VERSION = "2024-08-01-preview"
CHAT_URL = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_DEPLOYMENT}/chat/completions?api-version={API_VERSION}"

# ---- Helpers HTTP ----
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

# ---- Prompt do Revisor ----
SYSTEM_PROMPT = """Você é um revisor de código sênior. Avalie alterações em PRs com foco em:
- Bugs potenciais, regressões, condições de corrida;
- Segurança (injeções, secrets, validação, authZ/authN);
- Qualidade (nomenclatura, complexidade, duplicação, SOLID/Clean Code);
- Performance (alocação, I/O, queries, async/await, paralelismo);
- Testabilidade (cobertura, casos faltantes, mocks);
- Estilo e consistência (linters, convenções do projeto).

Responda sucintamente, mas com exemplos concretos (trechos de código em blocos).
Quando possível, sugira diffs com ```suggestion``` para facilitar o apply no GitHub.

IMPORTANTE — FORMATO DE SUGESTÕES APLICÁVEIS:
Ao final da sua resposta, inclua UM bloco ```json com o seguinte objeto:

{
  "suggestions":[
    {
      "path": "<nome do arquivo exato>",
      "note": "<breve explicação do porquê da mudança>",
      "original_snippet": "<trecho exato que existe HOJE no arquivo (use o trecho completo a substituir, linhas contíguas)>",
      "replacement": "<novo trecho para substituir o original exatamente na mesma região>"
    }
  ]
}

Regras:
- O "original_snippet" deve existir literalmente no conteúdo atual do arquivo.
- Use trechos curtos e objetivos (geralmente até ~20 linhas).
- Se não houver sugestões aplicáveis, retorne "suggestions": [].
- NÃO invente caminhos/trechos que não existem.
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
    # Mostre diff e trechos relevantes do arquivo (evita mandar tudo)
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

    Lembre-se de finalizar com o bloco JSON "suggestions" conforme o formato solicitado no sistema.
    """)

def post_review_comment(markdown_body: str):
    # Publica um único review consolidado (comentário geral)
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}/reviews"
    payload = {"body": markdown_body, "event": "COMMENT"}
    gh_post(url, payload)

# NOVO: cria um review com comentários ancorados + corpo consolidado
def post_batch_review_with_comments(markdown_body: str, comments: List[Dict]):
    """
    Publica um review do PR com um corpo consolidado e uma lista de comentários
    ancorados em linhas específicas do diff (side RIGHT), cada um contendo um bloco
    ```suggestion``` para permitir "Commit suggestion".
    """
    if not comments:
        return post_review_comment(markdown_body)

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}/reviews"
    payload = {
        "body": markdown_body,
        "event": "COMMENT",
        "comments": comments
    }
    gh_post(url, payload)

def get_diff_for_file(file_json: Dict) -> str:
    # utiliza o patch fornecido pela própria API de PR files
    return file_json.get("patch", "")

# NOVO: utilitário para parsear o bloco JSON de sugestões no final da resposta do modelo
def extract_suggestions_json(text: str) -> List[Dict]:
    """
    Busca o último bloco ```json ... ``` e tenta carregar como objeto com "suggestions".
    Se não encontrar ou falhar, retorna [].
    """
    code_blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if not code_blocks:
        return []
    for raw in reversed(code_blocks):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "suggestions" in obj and isinstance(obj["suggestions"], list):
                return obj["suggestions"]
        except Exception:
            continue
    return []

# NOVO: encontra linhas (start_line, end_line) de um snippet no conteúdo (1-based)
def find_snippet_line_span(content: str, snippet: str) -> Optional[Tuple[int, int]]:
    """
    Procura o snippet exato no conteúdo; se achar, retorna (start_line, end_line) 1-based.
    Caso múltiplas ocorrências, usa a primeira.
    """
    if not snippet:
        return None
    # normaliza quebras para evitar divergências \r\n vs \n
    content_norm = content.replace("\r\n", "\n")
    snippet_norm = snippet.replace("\r\n", "\n")
    idx = content_norm.find(snippet_norm)
    if idx == -1:
        return None
    start_line = content_norm.count("\n", 0, idx) + 1
    lines = snippet_norm.split("\n")
    end_line = start_line + len(lines) - 1
    return start_line, end_line

# NOVO: (opcional) valida se a região sugerida intersecta alterações do patch
def build_changed_line_ranges_from_patch(patch_text: str) -> List[Tuple[int, int]]:
    """
    Retorna ranges (start,end) 1-based de linhas alteradas no alvo (arquivo da direita).
    Usado como heurística pra priorizar comentários em linhas realmente alteradas.
    """
    ranges = []
    try:
        ps = PatchSet(patch_text.splitlines(keepends=True))
        for patched_file in ps:
            for h in patched_file:
                # h.target_lines() inclui +, ' ' e contextos; vamos pegar o intervalo alvo
                start = h.target_start
                end = h.target_start + h.target_length - 1
                if h.target_length > 0:
                    ranges.append((start, end))
    except Exception:
        pass
    return ranges

def intersects_any(span: Tuple[int,int], ranges: List[Tuple[int,int]]) -> bool:
    s1, e1 = span
    for s2, e2 in ranges:
        if not (e1 < s2 or e2 < s1):
            return True
    return False

# ---- Execução ----
files = fetch_pr_files()
if not files:
    post_review_comment("🤖 Nenhum arquivo relevante para revisão automática (extensões filtradas).")
    sys.exit(0)

all_sections = []
all_review_comments: List[Dict] = []  # NOVO: comentários por-linha com suggestion

for f in files:
    filename = f["filename"]
    patch = get_diff_for_file(f) or ""
    if not patch.strip():
        continue  # pula binários/renomeações sem alteração

    head_sha = f.get("sha")
    content = ""
    if head_sha:
        try:
            content = fetch_file_content(head_sha)
        except Exception:
            content = ""

    prompt = build_file_prompt(filename, patch, content)
    chunks = split_chunks(prompt, max_chars=7000)

    file_feedback_parts = []
    file_suggestions_collected: List[Dict] = []  # NOVO: sugestões parseadas desse arquivo

    for i, chunk in enumerate(chunks, start=1):
        messages = [
            {"role":"system", "content": SYSTEM_PROMPT},
            {"role":"user", "content": chunk}
        ]
        try:
            ans = call_aoai(messages)
            file_feedback_parts.append(ans.strip())

            # NOVO: tenta extrair sugestões estruturadas deste chunk
            suggestions = extract_suggestions_json(ans)
            for s in suggestions:
                # filtra sugestões que pertencem a este arquivo
                if s.get("path") == filename and s.get("original_snippet") and s.get("replacement"):
                    file_suggestions_collected.append(s)
        except Exception as e:
            file_feedback_parts.append(f"Falha ao analisar este bloco ({i}): {e}")

    # NOVO: converter sugestões em comentários ancorados com bloco ```suggestion```
    if file_suggestions_collected and content:
        changed_ranges = build_changed_line_ranges_from_patch(patch)

        for s in file_suggestions_collected:
            original = s.get("original_snippet","")
            replacement = s.get("replacement","")
            note = s.get("note","Sugestão de melhoria.")

            span = find_snippet_line_span(content, original)
            if not span:
                # não encontrou o snippet — deixa apenas no consolidado
                continue

            start_line, end_line = span
            # Heurística: se não intersecta alterações, ainda assim permitimos (pode ser refino em região não alterada)
            intersects = intersects_any(span, changed_ranges)

            # Corpo do comentário com explicação + bloco suggestion
            body = f"{note}\n\n```suggestion\n{replacement}\n```"

            # Comentário multiline quando necessário
            comment_payload = {
                "path": filename,
                "side": "RIGHT",
                "line": end_line  # linha final ancorada
            }
            if end_line > start_line:
                comment_payload["start_line"] = start_line
                comment_payload["start_side"] = "RIGHT"

            comment_payload["body"] = body
            all_review_comments.append(comment_payload)

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

# NOVO: se existirem sugestões estruturadas, publica review com comentários por-linha (aplicáveis)
post_batch_review_with_comments(body, all_review_comments)
print("AI review posted (with suggestions when available).")
