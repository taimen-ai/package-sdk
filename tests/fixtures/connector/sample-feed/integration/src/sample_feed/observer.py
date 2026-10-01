"""Наблюдатель-фикстура: записи ленты — из config.items (источник в тесте), курсор —
в state, токен — секрет узла."""

from package_sdk.connector import Observation, ObserveContext, observer, run


@observer(kind="sample-feed-observer", entrypoint="sample_feed.observer:observe")
def observe(ctx: ObserveContext) -> None:
    ctx.secret("sample-feed-token")
    cursor = int(ctx.state.get("cursor", 0))
    items = list(ctx.config.get("items") or [])
    page = int(ctx.config.get("pageSize") or 2)
    for item in items[cursor : cursor + page]:
        ctx.emit(
            Observation(
                kind="sample_feed.item_seen",
                dedup_key=f"sample-feed:{item['id']}",
                data=item,
                external_ref={"system": "sample-feed", "id": str(item["id"])},
            )
        )
        cursor += 1
    ctx.state["cursor"] = cursor


if __name__ == "__main__":
    run(observe)
