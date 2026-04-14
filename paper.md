---
title: "1bcoder: An AI Coding Assistant Designed for Small Local Language Models"
authors:
  - name: Stanislav Zholobetskyi
    orcid: 0009-0008-6058-7233
    affiliation: 1
affiliations:
  - name: Institute for Information Recording, Kyiv, Ukraine
    index: 1
date: 2026-04-14
bibliography: paper.bib
---

# Summary

`1bcoder` is a terminal-based AI coding assistant that runs entirely on a developer's local machine using small language models (0.5B–4B parameters) served through Ollama, LM Studio, or any OpenAI-compatible backend. It provides a command system for surgical context preparation, multi-agent parallel inference, parameterized proc scripts, and a project context library — all designed around the constraints of models that are too small to navigate a codebase autonomously but are entirely capable of editing, explaining, and transforming pre-prepared code.

# Statement of need

Contemporary AI coding assistants such as GitHub Copilot, Cursor, Cline, and Continue.dev are engineered for large cloud-hosted language models with context windows of 100,000 tokens or more. Their system prompts alone often consume more tokens than a 1B-parameter model's entire context window. Their tool-calling protocols assume reliable JSON generation and multi-step planning — capabilities that emerge only at larger scales. As a result, small local models are treated as unsuitable for agentic coding tasks and excluded from existing tooling.

This creates a practical problem. Many development environments prohibit sending code to external APIs due to contractual, regulatory, or security requirements [@ziegler2022productivity]. Locally-hosted open models address this constraint but lack the scaffolding that makes them productive. `1bcoder` fills this gap by providing a tool that is explicitly designed for the limitations of small models: human-directed context preparation replaces autonomous exploration, minimal and tolerant agent prompts replace complex multi-tool protocols, and output post-processing handles malformed responses that small models commonly produce.

# Functionality

**Context management** (`/ctx`, `/proj`): developers load specific files, log excerpts, command outputs, and saved context snapshots into the prompt with precise token-level control. Context can be saved to named libraries and restored across sessions, enabling reuse of prepared contexts across related tasks.

**Agent system** (`/agent`): five single-purpose agents — `ask`, `edit`, `fill`, `scan`, `compact` — each with at most five tools and a single responsibility. Short, structured system prompts keep the model focused and within context limits.

**Parallel inference** (`/parallel`): the same context is sent simultaneously to multiple models or providers and results are collected for comparison. Supports local models on multiple machines, enabling practical coordination of very small models.

**Proc scripts** (`/proc`): parameterized command scripts that encode repeatable context-preparation workflows. A proc can read files, run shell commands, apply filters, and stage a complete context for a model query.

**Map integration** (`/map`): integration with `svitovyd` [@svitovyd] provides a structural project map — definitions, cross-file dependencies, call graphs — that lets the model navigate a codebase without loading it into context.

**MCP support**: `1bcoder` acts as an MCP client, connecting to any MCP server (including `simargl` [@simargl] and `svitovyd`) to give the model access to retrieval and structural analysis tools during a session.

# Related software

`aider` [@aider] and `Continue.dev` [@continue] are the closest alternatives but both assume models capable of reliable tool use and handle large context windows by default. Neither provides the context library, parallel inference, or proc scripting features of `1bcoder`. `Shell-AI` addresses command generation only. No existing tool targets the 0.5B–4B model tier with an explicit context preparation discipline.

# Acknowledgements

This work is conducted as part of a PhD research programme at the Institute for Information Recording, Kyiv, Ukraine.

# References
