from src.analyst.graph import build_graph
from src.common import langsmith, slack
from src.common.config import load_config
from src.common.logging import get_logger

log = get_logger("ANALYST")


def main():
    cfg = load_config()
    langsmith.configure(cfg)

    try:
        graph = build_graph()
        result = graph.invoke(
            {"raw_candidates": [], "research_text": "", "selection": None},
            config={"tags": ["analyst"]},
        )
        selection = result.get("selection") or {}
        log(f"wrote portfolio with {len(selection.get('symbols', []))} symbols")
    except Exception as exc:
        log(f"💥 Analyst run failed: {exc}")
        slack.notify_error("ANALYST", str(exc))
        raise


if __name__ == "__main__":
    main()
