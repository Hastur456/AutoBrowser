# Eval Platform Guide (Langfuse)

How AutoBrowser's local batch results become Langfuse datasets, experiments and scores, and how
to read them there. The decision record is
[ADR-2026-10-31: Langfuse as the Dataset, Experiment and Score Platform](../decisions/2026-10-31-langfuse-platform-integration.md);
the design is [docs/development/2026-10-31-langfuse-integration-design.md](2026-10-31-langfuse-integration-design.md).

The local batch folder stays the source of truth and the only source of the regression verdict;
Langfuse is a second interface over it. This guide is filled in as the branch
`feat/langfuse-sync` lands (steps L1–L10 of the design).

## Connecting (Cloud and self-host)

## Keys, Content Capture and Privacy

## Datasets and Versions

## Experiments

## Scores

## LLM Judge

## Annotation Queue

## Commands

## Recovery and Troubleshooting
