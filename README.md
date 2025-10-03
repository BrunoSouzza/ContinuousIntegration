# Documentação de Workflows e Scripts de CI (.github)

Este repositório foi criado com o objetivo principal de demonstrar e configurar uma pipeline de **Integração Contínua (CI)** utilizando o GitHub Actions para projetos .NET, incluindo automações de build, testes, verificação de cobertura de código e até mesmo revisão automatizada de código com IA.

## Estrutura da Pasta `.github`

A pasta `.github` contém arquivos vitais para a automação do ciclo de desenvolvimento, divididos principalmente em:

- **Workflows**: Arquivos YAML em `.github/workflows/` definem as pipelines que são executadas automaticamente pelo GitHub Actions.
- **Scripts**: Scripts auxiliares em `.github/scripts/`, usados pelos workflows.

---

## Workflows

### 1. `.github/workflows/dotnet-desktop.yml`

Este workflow é responsável por automatizar o build, teste e análise de cobertura de código para projetos .NET. Ele é disparado tanto em pushes quanto em pull requests para a branch `main`.

**Etapas principais:**
- **Checkout:** Faz o checkout do código fonte.
- **Setup .NET:** Instala a versão especificada do SDK do .NET.
- **Restore:** Restaura as dependências do projeto.
- **Build:** Compila a solução no modo Release.
- **Testes e Cobertura:** Executa os testes automatizados e coleta a cobertura de código.
- **Relatório de Cobertura:** Gera um resumo textual da cobertura usando o ReportGenerator.
- **Validação de Threshold:** Checa se a cobertura de linha está acima de um mínimo configurável (exemplo: 60%). O workflow falha caso a cobertura esteja abaixo desse valor, forçando a manutenção da qualidade do código.

<details>
<summary>Ver workflow completo</summary>

[.github/workflows/dotnet-desktop.yml](https://github.com/BrunoSouzza/ContinuousIntegration/blob/main/.github/workflows/dotnet-desktop.yml)
</details>

---

### 2. `.github/workflows/ai-code-review.yml`

Este workflow implementa uma revisão automatizada de código baseada em IA utilizando o Azure OpenAI. Ele é disparado em eventos de pull request (quando um PR é aberto, atualizado, reaberto ou marcado como pronto para revisão).

**Etapas principais:**
- **Checkout e Setup Python:** Prepara o ambiente para execução do script de IA.
- **Instalação de dependências Python:** Instala bibliotecas necessárias como `requests`, `pygments` e `unidiff`.
- **Execução do Script de Revisão:** Roda o script Python localizado em `.github/scripts/ai_review.py`, que:
    - Busca arquivos modificados relevantes no PR.
    - Interage com o serviço do Azure OpenAI para analisar as alterações.
    - Publica comentários automatizados (inline e consolidados) diretamente no PR, sugerindo melhorias ou apontando possíveis problemas.

**Configurações sensíveis** (via segredos do repositório) são usadas para autenticação no GitHub e no Azure OpenAI.

<details>
<summary>Ver workflow completo</summary>

[.github/workflows/ai-code-review.yml](https://github.com/BrunoSouzza/ContinuousIntegration/blob/main/.github/workflows/ai-code-review.yml)
</details>

---

## Scripts

### `.github/scripts/ai_review.py`

Este script Python faz toda a lógica da revisão automatizada por IA, sendo chamado pelo workflow descrito acima. Suas funções incluem:

- Carregar informações do pull request via variáveis de ambiente e arquivos de contexto do GitHub Actions.
- Buscar arquivos alterados que são relevantes para revisão (por exemplo, C#, YAML, JSON, etc).
- Baixar o conteúdo dos arquivos alterados para análise.
- Fazer chamadas ao Azure OpenAI (modelo GPT) para gerar sugestões e análises.
- Publicar comentários e sugestões diretamente no PR, podendo inclusive sugerir trechos de código para aplicação automática.

O script é altamente configurável via variáveis de ambiente definidas no workflow.

<details>
<summary>Ver script completo</summary>

[.github/scripts/ai_review.py](https://github.com/BrunoSouzza/ContinuousIntegration/blob/main/.github/scripts/ai_review.py)
</details>

---

## Objetivo do Repositório

O propósito deste repositório é **demonstrar uma configuração completa de Integração Contínua (CI) para projetos .NET no GitHub**, com workflows que automatizam desde o build e testes até a análise de cobertura e revisão automatizada de código por IA. A configuração serve como exemplo prático de como estruturar pipelines modernas e eficientes usando GitHub Actions, promovendo qualidade e agilidade no desenvolvimento.

---

> **Dica:** Todos os workflows e scripts podem ser adaptados para outros projetos .NET ou ampliados conforme a necessidade.

---

**Veja todos arquivos e detalhes em:**  
[.github/ na interface do GitHub](https://github.com/BrunoSouzza/ContinuousIntegration/tree/main/.github)
