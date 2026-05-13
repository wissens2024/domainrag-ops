# Evaluation Compare — A: 7B-AWQ baseline vs B: 7B fp16 (가성비)

## Summary metrics

| Metric | A | B | Δ (B − A) |
|---|---:|---:|---:|
| total_cases | 5 | 5 | +0
| retrieval_recall_at_5 | 1.0000 | 1.0000 | +0.0000
| citation_accuracy | 0.8000 | 0.8000 | +0.0000
| unsupported_ratio | 0.0000 | 0.0000 | +0.0000
| fallback_rate | 0.0000 | 0.0000 | +0.0000
| pass_count | 5 | 5 | +0
| fail_count | 0 | 0 | +0

## Gate result

| 항목 | A | B |
|---|---|---|
| passed | False | False |
| failed metrics | citation_accuracy | citation_accuracy |

## Wall-clock latency

- A: 0.71s (EVAL-1AB4568A805741C1)
- B: 0.43s (EVAL-A539C1B29E244FF5)
- Δ: -0.28s

## Decision hint

**B 채택 권장** — fallback_rate 증가 ≤ 5%p, citation_accuracy 손실 ≤ 5%p, latency 단축. 모델 weight 단일화 효과 확보.