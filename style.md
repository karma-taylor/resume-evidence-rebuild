# Rollout logging and bounded-edit format

## Private failure trajectories

Store each failed A4 run under a private runtime directory. The JSON record contains hashes, relative references, the current public rule snapshot and structured QA findings; authorized raw materials are copied into an access-restricted `inputs/` subdirectory and never committed.

```json
{
  "schema_version": "1.1",
  "event_type": "a4_qa_failed",
  "run": {"run_id": "rollout-...", "skill": {"sha256": "..."}},
  "qa": {"passed": false, "findings": [{"code": "BOTTOM_WHITESPACE_EXCESS", "element_id": "project.rag.bullet.3"}]}
}
```

## Bounded patch envelope

The optimizer returns an RFC 6902 patch against only these section strings: `workflow`, `output-routing`, and `one-page-layout-qa`. It may use `add`, `remove`, or `replace`, with at most three operations and a combined character delta of 450. It cannot edit frontmatter, evidence/privacy rules, schemas, templates, benchmark definitions, identity data or sources.

## Bullet payload

Every generated project bullet is an object:

```json
{
  "text": "一条介于六十至七十个中文字符、仅含已授权事实的项目描述。",
  "bold_phrases_used": ["已验证控制规则"],
  "source_claim_ids": ["claim-id"]
}
```

`text` contains **60–70 CJK characters**; `bold_phrases_used` contains one or two strings that occur verbatim in `text`; each phrase must be recoverable through `source_claim_ids` to an authorized claim. The renderer must make those exact phrases visually bold.
