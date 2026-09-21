import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_EVAL_SET = os.path.join(PROJECT_ROOT, "evals", "eval_set.json")

JUDGE_PROMPT = (
    "You are a strict factuality judge. Given a QUESTION, the RETRIEVED "
    "CONTEXT, and an ANSWER produced by an assistant using only that context, "
    "decide whether the ANSWER is fully supported by the CONTEXT and directly "
    "answers the QUESTION. Respond with exactly YES or NO."
)


def load_eval_set(path=DEFAULT_EVAL_SET):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("questions", [])
    return [
        {
            "question": row["question"],
            "expected_sources": sorted(set(row.get("expected_sources", []))),
            "answer_hint": row.get("answer_hint", ""),
        }
        for row in data
    ]


def hit_rate(result_sources, expected):
    return any(s in expected for s in result_sources)


def recall_at_k(result_sources, expected):
    if not expected:
        return 1.0
    return len(set(result_sources) & set(expected)) / len(expected)


def reciprocal_rank(result_sources, expected):
    for i, source in enumerate(result_sources):
        if source in expected:
            return 1.0 / (i + 1)
    return 0.0


def aggregate_metrics(examples):
    n = len(examples)
    return {
        "questions": n,
        "hit_rate": sum(e["hit"] for e in examples) / n,
        "recall": sum(e["recall"] for e in examples) / n,
        "mrr": sum(e["mrr"] for e in examples) / n,
    }


def sources_of(results):
    return [r.source for r in results]


def run_retrieval_eval(retrieve_fn, questions, k=3):
    examples = []
    for item in questions:
        results = retrieve_fn(item["question"], k=k)
        sources = sources_of(results)
        examples.append(
            {
                "question": item["question"],
                "expected_sources": item["expected_sources"],
                "retrieved_sources": sources,
                "hit": hit_rate(sources, item["expected_sources"]),
                "recall": recall_at_k(sources, item["expected_sources"]),
                "mrr": reciprocal_rank(sources, item["expected_sources"]),
            }
        )
    return examples


def judge_faithful(client, model_name, question, context, answer):
    ask = (
        f"QUESTION: {question}\n\nRETRIEVED CONTEXT:\n{context}\n\n"
        f"ASSISTANT ANSWER:\n{answer}\n\nIs the answer fully supported by the "
        "context? Reply exactly YES or NO."
    )
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": ask},
        ],
        temperature=0,
        max_tokens=5,
    )
    return response.choices[0].message.content.strip().upper().startswith("YES")


def run_answer_eval(bot, questions, k=3, limit=None):
    if limit:
        questions = questions[:limit]
    faithful = 0
    verdicts = []
    for item in questions:
        results = bot.retrieve(question=item["question"], k=k)
        context = "\n--\n".join(f"[{r.source}] {r.content}" for r in results)
        answer = bot.ask(question=item["question"], k=k)
        ok = judge_faithful(bot.openai_client, bot.model_name, item["question"], context, answer)
        faithful += ok
        verdicts.append({"question": item["question"], "answer": answer, "faithful": ok})
    return {"faithfulness": faithful / len(questions), "verdicts": verdicts}


def main(argv=None):
    parser = argparse.ArgumentParser(description="ESG RAG retrieval/answer evaluation")
    parser.add_argument("--set", default=DEFAULT_EVAL_SET, help="path to eval set JSON")
    parser.add_argument("--k", type=int, default=3, help="retrieval depth")
    parser.add_argument("--limit", type=int, default=None, help="only first N questions")
    parser.add_argument("--judge", action="store_true", help="also run LLM answer judge")
    parser.add_argument("--json", dest="json_path", help="write JSON report to this path")
    parser.add_argument("--min-hit-rate", type=float, default=0.0)
    parser.add_argument("--min-recall", type=float, default=0.0)
    parser.add_argument("--min-mrr", type=float, default=0.0)
    parser.add_argument("--min-faithfulness", type=float, default=0.0)
    args = parser.parse_args(argv)

    from chatbot.rag import RAGBot

    bot = RAGBot()
    if bot.store.count() == 0:
        print("index empty - ingesting data/ ...")
        bot.ingest()

    questions = load_eval_set(args.set)
    if args.limit:
        questions = questions[: args.limit]

    examples = run_retrieval_eval(bot.retrieve, questions, k=args.k)
    metrics = aggregate_metrics(examples)
    report = {"k": args.k, **metrics, "examples": examples}

    print(f"\nretrieval@{args.k} ({metrics['questions']} questions)")
    for e in examples:
        flag = "OK " if e["hit"] else "MISS"
        print(
            f"  [{flag}] {e['question'][:70]:<70} -> {sorted(set(e['retrieved_sources']))}"
        )
    print(
        f"  hit_rate={metrics['hit_rate']:.3f}  recall={metrics['recall']:.3f}  mrr={metrics['mrr']:.3f}"
    )

    if args.judge:
        answer = run_answer_eval(bot, questions, k=args.k)
        report["answer"] = answer
        print(f"  faithfulness={answer['faithfulness']:.3f}")
        for v in answer["verdicts"]:
            print(f"  [{('OK' if v['faithful'] else 'FAIL')}] {v['question'][:70]}")

    if args.json_path:
        with open(args.json_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nreport written to {args.json_path}")

    failures = []
    if metrics["hit_rate"] < args.min_hit_rate:
        failures.append(
            f"hit_rate {metrics['hit_rate']:.3f} < {args.min_hit_rate}"
        )
    if metrics["recall"] < args.min_recall:
        failures.append(f"recall {metrics['recall']:.3f} < {args.min_recall}")
    if metrics["mrr"] < args.min_mrr:
        failures.append(f"mrr {metrics['mrr']:.3f} < {args.min_mrr}")
    if args.judge and report.get("answer", {}).get("faithfulness", 1.0) < args.min_faithfulness:
        failures.append(
            f"faithfulness {report['answer']['faithfulness']:.3f} < {args.min_faithfulness}"
        )

    if failures:
        print("\nEVAL FAILED:", "; ".join(failures))
        return 1
    print("\nEVAL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())