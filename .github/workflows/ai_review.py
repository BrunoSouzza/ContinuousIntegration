import os, sys, json, math, textwrap, base64, fnmatch, subprocess
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

# ---- Variáveis de controle por ENV (com defaults sensatos) ----
SUGGEST_INLINE = os.getenv("SUGGEST_INLINE", "true").lower() == "true"
ONLY_CONSOLIDATED = os.getenv("ONLY_CONSOLIDATED", "false").lower() == "true"
MAX_INLINE_COMMENTS = int(os.getenv("MAX_INLINE_COMMENTS", "20"))
APPLY_MODE = os.getenv("APPLY_MODE", "suggestions").lower()  # "suggestions" | "commit"
EXCLUDE_GLOBS = [g.strip() for g in os.getenv("EXCLUDE_GLOBS", "").split(",") if g.strip()]

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
head_sha = pr["head"]["sha"]
pr_branch_ref = pr["head"]["ref"]  # ex.: feature/xyz

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
    
def gh_post_raw(url: str, raw_json: str):
    r = requests.post(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept":"application/vnd.github+json", "Content-Type":"application/json"}, data=raw_json)
    r.raise_for_status()
    return r.json()

# ---- Util ----
def should_review(filename: str) -> bool:
    fn = filename.lower()
    return any(fn.endswith(ext) for ext in REVIEW_FILE_EXTS)

def fetch_pr_files_all_pages() -> List[Dict]:
    # paginação robusta para PRs grandes
    files = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}/files?per_page=100&page={page}"
        batch = gh_get(url)
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return [f for f in files if should_review(f.get("filename","")) and f.get("status") != "removed"]

def fetch_file_content(sha: str) -> str:
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/git/blobs/{sha}"
    data = gh_get(url)
    if data.get("encoding") == "base64":
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return data.get("content","") or ""

def split_chunks(text: str, max_chars: int = 7000) -> List[str]:
    # corte simples por tamanho para evitar janelas muito grandes
    if len(text) <= max_chars:
        return [text]
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i+max_chars])
        i += max_chars
    return chunks

# ---------------- Prompt ----------------
SYSTEM_PROMPT = """Você é um revisor de código sênior. Avalie alterações em PRs com foco em:
- Bugs potenciais, regressões, condições de corrida;
- Segurança (injeções, secrets, validação, authZ/authN);
- Qualidade (nomenclatura, complexidade, duplicação, SOLID/Clean Code);
- Performance (alocação, I/O, queries, async/await, paralelismo);
- Testabilidade (cobertura, casos faltantes, mocks);
- Estilo e consistência (linters, convenções do projeto).

Responda em **JSON estrito** no formato:
{
  "summary": "texto curto com os principais achados (bullets aceitos)",
  "suggestions": [
    {
      "path": "<mesmo path do arquivo>",
      "start_line": <int, linha no arquivo após as mudanças (lado RIGHT)>,
      "end_line": <int, inclusive, >= start_line>,
      "replacement": "<APENAS o novo conteúdo que deve substituir o trecho>",
      "rationale": "por que mudar; evite texto longo"
    }
  ]
}

Regras para suggestions:
- Foque em FAIXAS MÍNIMAS necessárias, e que existam no diff atual.
- Use indentação coerente do projeto.
- O campo "replacement" NÃO deve conter cercas de código (sem ```), nem comentários adicionais.
- Se não houver sugestões críticas, retorne "suggestions": [].
"""

def build_file_prompt(filename: str, patch: str, content: str) -> str:
    preview = content[:3000]
    diff_snippet = patch[:6000]
    return textwrap.dedent(f"""
    Arquivo: {filename}

    DIFF (unified):
    ```
    {diff_snippet}
    ```

    Trecho do conteúdo atual (início):
    ```
    {preview}
    ```

    Tarefa:
    - Gere um objeto JSON exatamente no formato pedido no system prompt.
    - Se criar sugestões, garanta linhas válidas (lado RIGHT) e replacement mínimo.
    """)

# ---------------- Azure OpenAI ----------------
def call_aoai(messages: List[Dict], temperature: float = 0.2) -> str:
    payload = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"}
    }
    r = requests.post(
        CHAT_URL,
        headers={"api-key": AOAI_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=120
    )
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

# ---------------- Diff helpers ----------------
def parse_new_file_lines_from_patch(patch: str) -> Tuple[int, int, set]:
    """
    Retorna (min_right, max_right, right_line_set) com linhas válidas do lado RIGHT conforme o patch.
    """
    right_lines = set()
    min_r, max_r = None, None

    if not patch.strip():
        return (0, 0, set())

    ps = PatchSet(patch, encoding='utf-8', errors='replace')
    for pfile in ps:
        for hunk in pfile:
            right_line_num = hunk.target_start
            # percorre linhas do hunk
            for line in hunk:
                if line.is_added or line.is_context:
                    # linhas que existem no lado RIGHT
                    if right_line_num is not None:
                        right_lines.add(right_line_num)
                        if min_r is None or right_line_num < min_r:
                            min_r = right_line_num
                        if max_r is None or right_line_num > max_r:
                            max_r = right_line_num
                    right_line_num += 1
                elif line.is_removed:
                    # remoções não avançam o RIGHT
                    pass
    if min_r is None:
        min_r, max_r = 0, 0
    return (min_r, max_r, right_lines)

def clamp_and_validate_range(start_line: int, end_line: int, right_lines: set, min_r: int, max_r: int) -> Tuple[int, int, bool]:
    if start_line > end_line:
        start_line, end_line = end_line, start_line
    start_line = max(min_r, start_line)
    end_line = min(max_r, end_line)
    if start_line > end_line:
        return (start_line, end_line, False)
    # pelo menos uma linha do intervalo deve estar no conjunto do diff
    valid_overlap = any((ln in right_lines) for ln in range(start_line, end_line + 1))
    return (start_line, end_line, valid_overlap)

def sanitize_replacement(repl: str) -> str:
    # Evita cercas que quebram o bloco suggestion
    return repl.replace("```", "``\u200b`").rstrip("\n")

# ---------------- Comentários no GitHub ----------------
def post_review_comment(markdown_body: str):
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}/reviews"
    payload = {"body": markdown_body, "event": "COMMENT"}
    gh_post(url, payload)

def post_review_with_inline_suggestions(commit_sha: str, comments: List[Dict[str, Any]]):
    """
    comments: [{ "path", "start_line", "end_line", "replacement", "body_extra" }]
    Publica um único review com múltiplos comentários inline (cada um com bloco suggestion).
    """
    if not comments:
        return
    review_comments = []
    for c in comments[:MAX_INLINE_COMMENTS]:
        body_text = "Sugestão automática:\n\n```suggestion\n" + sanitize_replacement(c["replacement"]) + "\n```\n"
        if c.get("body_extra"):
            body_text += f"\n{c['body_extra']}\n"
        item = {
            "path": c["path"],
            "side": "RIGHT",
            "line": c["end_line"],
            "body": body_text
        }
        if c["end_line"] != c["start_line"]:
            item["start_line"] = c["start_line"]
            item["start_side"] = "RIGHT"
        review_comments.append(item)

    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/pulls/{pr_number}/reviews"
    payload = {
        "event": "COMMENT",
        "comments": review_comments
    }
    gh_post(url, payload)

# ---------------- Aplicação via commit (opcional) ----------------
def git_run(args: List[str]):
    print("+ git", " ".join(args))
    subprocess.run(["git"] + args, check=True)

def apply_edits_and_commit(edits: Dict[str, List[Dict[str, Any]]], base_dir: str = "."):
    """
    Aplica localmente as sugestões (edits) nos arquivos e cria commit/push.
    edits: { "path": [ { "start_line", "end_line", "replacement" }, ... ] }
    """
    # Carrega e aplica por arquivo
    for path, changes in edits.items():
        target_path = os.path.join(base_dir, path)
        if not os.path.exists(target_path):
            print(f"[apply] arquivo não existe no checkout: {path}, ignorando.")
            continue
        with open(target_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()

        # aplicar do fim para o começo para não bagunçar offsets
        for ch in sorted(changes, key=lambda x: x["start_line"], reverse=True):
            s, e = ch["start_line"], ch["end_line"]
            # transformar em índices 0-based
            s_idx = max(0, s - 1)
            e_idx = min(len(lines) - 1, e - 1) if lines else -1
            new_block = ch["replacement"].splitlines()
            # substituir faixa
            if e_idx >= s_idx and 0 <= s_idx < len(lines):
                lines[s_idx:e_idx+1] = new_block
            else:
                print(f"[apply] range inválido em {path}: {s}-{e}, ignorando esse edit.")

        with open(target_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))

    # Configura autor e faz commit/push
    author_name = os.getenv("GIT_COMMIT_AUTHOR_NAME", "ai-review-bot")
    author_email = os.getenv("GIT_COMMIT_AUTHOR_EMAIL", "ai-review-bot@users.noreply.github.com")
    git_run(["config", "user.name", author_name])
    git_run(["config", "user.email", author_email])

    # Garante estar no branch da PR
    # (em pull_request o checkout pode estar detached; cria/usa branch local com mesmo nome)
    git_run(["checkout", "-B", pr_branch_ref])
    git_run(["add", "-A"])
    # só commita se houver alterações
    status = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if status.returncode == 0:
        print("[apply] Sem alterações a commitar.")
        return
    git_run(["commit", "-m", "chore(ai-review): aplicar sugestões automáticas"])
    # push de volta para o branch da PR
    git_run(["push", "origin", pr_branch_ref])

# ---------------- Execução principal ----------------
def main():
    files = fetch_pr_files_all_pages()
    if not files:
        post_review_comment("🤖 Nenhum arquivo relevante para revisão automática (extensões/filters).")
        print("No relevant files.")
        return

    consolidated_sections = []
    inline_comments: List[Dict[str, Any]] = []
    edits_to_apply: Dict[str, List[Dict[str, Any]]] = {}

    for f in files:
        filename = f["filename"]
        patch = f.get("patch") or ""
        if not patch.strip():
            continue  # ignora binários/renomeações sem diff

        content = ""
        head_blob_sha = f.get("sha")
        if head_blob_sha:
            try:
                content = fetch_file_content(head_blob_sha)
            except Exception:
                content = ""

        min_r, max_r, right_lines = parse_new_file_lines_from_patch(patch)

        # Monta prompt e chama modelo (pode quebrar em chunks se precisar)
        prompt = build_file_prompt(filename, patch, content)
        chunks = split_chunks(prompt, max_chars=7000)

        file_summaries = []
        file_suggestions = []

        for i, chunk in enumerate(chunks, start=1):
            messages = [
                {"role":"system", "content": SYSTEM_PROMPT},
                {"role":"user", "content": chunk}
            ]
            try:
                raw = call_aoai(messages)
                data = json.loads(raw)
                if isinstance(data, dict):
                    if "summary" in data:
                        file_summaries.append(str(data["summary"]))
                    if "suggestions" in data and isinstance(data["suggestions"], list):
                        file_suggestions.extend(data["suggestions"])
                else:
                    file_summaries.append(f"(chunk {i}) Resposta não-JSON válida.")
            except Exception as e:
                file_summaries.append(f"(chunk {i}) Falha ao analisar ({e})")

        # Consolida resumo do arquivo
        if file_summaries:
            section = f"### `{filename}`\n" + "\n".join(f"- {s}" for s in file_summaries if s.strip())
            consolidated_sections.append(section)

        # Processa sugestões → validação de linhas e preparação de inline/comments
        for s in file_suggestions:
            try:
                path = s.get("path") or filename
                start_line = int(s.get("start_line"))
                end_line = int(s.get("end_line"))
                replacement = str(s.get("replacement", ""))
                rationale = str(s.get("rationale", "")).strip()

                start_line, end_line, ok = clamp_and_validate_range(start_line, end_line, right_lines, min_r, max_r)
                if not ok or not replacement.strip():
                    continue

                # para publicar inline
                inline_comments.append({
                    "path": path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "replacement": replacement,
                    "body_extra": (f"**Racional:** {rationale}" if rationale else None)
                })

                # para modo commit
                edits_to_apply.setdefault(path, []).append({
                    "start_line": start_line,
                    "end_line": end_line,
                    "replacement": replacement
                })

            except Exception:
                continue

    # Publicação
    if SUGGEST_INLINE and inline_comments:
        post_review_with_inline_suggestions(head_sha, inline_comments)

    if not ONLY_CONSOLIDATED:
        if consolidated_sections:
            body = (
                "## 🤖 Azure OpenAI Code Review\n"
                f"- PR: #{pr_number}\n"
                f"- Escopo: arquivos filtrados por extensão; sugestões inline: {'sim' if SUGGEST_INLINE else 'não'}.\n\n"
                + "\n\n---\n\n".join(consolidated_sections)
            )
        else:
            body = "🤖 Consegui ler os arquivos, mas não havia *diff* textual analisável ou respostas relevantes."
        post_review_comment(body)

    # Modo commit (opcional)
    if APPLY_MODE == "commit" and inline_comments:
        try:
            apply_edits_and_commit(edits_to_apply, base_dir=".")
            post_review_comment("✅ Sugestões automáticas aplicadas via commit no branch da PR.")
        except Exception as e:
            post_review_comment(f"⚠️ Tentativa de aplicar sugestões via commit falhou: `{e}`")

    print("AI review finished.")

if __name__ == "__main__":
    main()
