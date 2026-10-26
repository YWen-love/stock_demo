"""Gradio 对话界面 + 盘点卡片。

卡片为什么是独立组件而不是塞进气泡里：
Gradio 6.27 的 Chatbot 支持往消息里塞组件，但源码里每条消息在渲染时都会
**新实例化**一份（chatbot.py 的 `_postprocess` 和 `_preprocess_content`），
服务端挂不上 `.input()` / `.change()` 监听。塞进去就变成一张只能看的死表，
店主改不了数字，整条闭环就断了。所以卡片做成对话流下方的独立组件。

卡片默认隐藏：平时就是纯对话。店主说「该盘点了」或者点按钮才展开，
提交完自动收起、摘要回写进对话流。这样"表格"降级成对话里的一个动作，
而不是一个需要单独进入的页面。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import gradio as gr
import pandas as pd

from . import onboarding
from .advisor import InventoryAdvisor

logger = logging.getLogger(__name__)

CARD_COLUMNS = ["商品", "单位", "上次盘点", "本次库存", "本周进货", "备注"]
CARD_DATATYPES = ["str", "str", "number", "number", "number", "str"]

OPEN_CARD = {"__type__": "update", "visible": True}
CLOSE_CARD = {"__type__": "update", "visible": False}
#: 空的 update = 这个组件这次别动。卡片里的数是店主手填的，
#: 问句一多就把它收走等于把人家填了一半的账扔了。
KEEP_CARD = {"__type__": "update"}


def rows_to_frame(rows: Sequence[Dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=CARD_COLUMNS)


def frame_to_payload(frame: Any) -> tuple[List[Dict[str, Any]], List[str]]:
    """Dataframe → 提交用的行列表，外加被跳过的空行商品名。

    这里只做格式转换，不碰数据库。名字能不能对上由调用方查库判断 ——
    猜错一个名字，销量推导会跟着错一整周，而且要等到下周才发现。
    """
    if frame is None:
        return [], []
    data = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)

    payload: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for _, row in data.iterrows():
        name = str(row.get("商品", "") or "").strip()
        if not name:
            skipped.append("(空行)")
            continue
        payload.append({
            "name": name,
            "stock": _to_float(row.get("本次库存")),
            "purchase_qty": _to_float(row.get("本周进货")),
            "note": str(row.get("备注", "") or "").strip() or None,
        })
    return payload, skipped


def _to_float(value: Any) -> float:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_demo(db_path: Any = None, client: Optional[Any] = None) -> gr.Blocks:
    advisor = InventoryAdvisor(db_path=db_path, client=client)

    def on_chat(message: str, history: List[Dict[str, Any]]):
        reply = advisor.reply(message, history=history)
        # 卡片只在真的要盘点的时候动。以前这里是每条消息都开一次卡，
        # 问「可乐该进多少」也弹出一张表；问完就把卡收走，店主填一半的数就没了。
        if reply.open_card:
            card, frame = OPEN_CARD, rows_to_frame(advisor.stocktake_rows())
        else:
            card, frame = KEEP_CARD, KEEP_CARD
        # 历史要同时回写到 chatbot 和 state：chatbot 是给人看的，state 是给下一轮当上下文的。
        # 只回写 chatbot 的话，下一轮拿到的 history 还是空的，模型就失忆了。
        new_history = list(history) + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply.text},
        ]
        return "", new_history, new_history, card, frame

    def on_open_card():
        return OPEN_CARD, rows_to_frame(advisor.stocktake_rows())

    def on_prefill():
        return rows_to_frame(advisor.prefill_by_average())

    def on_submit_card(frame: Any, history: List[Dict[str, Any]]):
        payload, skipped = frame_to_payload(frame)
        if not payload:
            new_history = list(history) + [{
                "role": "assistant",
                "content": "卡片里没读到数据，先点一下「🧾 本周盘点」重新打开。",
            }]
            return CLOSE_CARD, new_history, new_history

        rows, missing = [], list(skipped)
        for item in payload:
            product = advisor_product(advisor, item["name"])
            if product is None:
                missing.append(item["name"])
                continue
            rows.append({
                "product_id": product["product_id"],
                "stock": item["stock"],
                "purchase_qty": item["purchase_qty"],
                "note": item["note"],
            })

        new_history = list(history)
        if missing:
            new_history.append({
                "role": "assistant",
                "content": f"没认出这几个名字，已跳过：{'、'.join(missing)}。"
                           f"要加新商品的话直接跟我说就行。",
            })
        if rows:
            reply = advisor.submit_stocktake(rows)
            new_history.append({"role": "assistant", "content": reply.text})
        return CLOSE_CARD, new_history, new_history

    def on_cancel():
        return CLOSE_CARD

    def on_reset(history: List[Dict[str, Any]]):
        advisor.reset()
        greeting = [{"role": "assistant", "content": onboarding.GREETING}]
        return greeting, CLOSE_CARD, rows_to_frame(advisor.stocktake_rows()), greeting

    def on_load():
        text = (onboarding.GREETING if onboarding.needs_onboarding(db_path=db_path)
                else _welcome(advisor))
        greeting = [{"role": "assistant", "content": text}]
        return greeting, greeting

    with gr.Blocks(title="库存顾问") as demo:
        state = gr.State([])

        gr.Markdown("### 🧾 库存顾问\n跟它说你想进什么，它告诉你进多少。")

        chatbot = gr.Chatbot(label="对话", height=460, buttons=["copy"])

        gr.Markdown("---")

        open_btn = gr.Button("🧾 本周盘点", variant="primary", size="sm")

        with gr.Column(visible=False, elem_id="stocktake_card") as card:
            gr.Markdown("**填一下这周的数** —— 跟上次一样的格子不用改，只改动过的。")
            table = gr.Dataframe(
                headers=CARD_COLUMNS,
                datatype=CARD_DATATYPES,
                interactive=True,
                wrap=True,
                column_widths=[130, 56, 90, 90, 90, 140],
                type="pandas",
                label="本周盘点",
            )
            with gr.Row():
                prefill_btn = gr.Button("按近4周均值预填", size="sm")
                submit_btn = gr.Button("提交盘点", variant="primary", size="sm")
                cancel_btn = gr.Button("取消", size="sm")

        with gr.Row():
            chat_input = gr.Textbox(show_label=False, placeholder="想进什么货？问我就行…",
                                    scale=20, container=False)
            send_btn = gr.Button("发送", variant="primary", scale=1, min_width=80)
            reset_btn = gr.Button("清库重来", scale=1, min_width=90)

        # api_visibility="private"：Gradio 6 去掉了全局的 show_api 参数，
        # 改成按事件控制。这个是本机工具，不该把接口挂出去。
        open_btn.click(on_open_card, outputs=[card, table], api_visibility="private")
        chat_input.submit(on_chat, inputs=[chat_input, state],
                          outputs=[chat_input, chatbot, state, card, table],
                          api_visibility="private")
        send_btn.click(on_chat, inputs=[chat_input, state],
                       outputs=[chat_input, chatbot, state, card, table],
                       api_visibility="private")
        prefill_btn.click(on_prefill, outputs=[table], api_visibility="private")
        submit_btn.click(on_submit_card, inputs=[table, state],
                         outputs=[card, chatbot, state], api_visibility="private")
        cancel_btn.click(on_cancel, outputs=[card], api_visibility="private")
        reset_btn.click(on_reset, inputs=[state], outputs=[chatbot, card, table, state],
                        api_visibility="private")

        demo.load(on_load, outputs=[chatbot, state], api_visibility="private")

    return demo


def advisor_product(advisor: InventoryAdvisor, name: str):
    """名字 → 商品。查不到就返回 None，让上层提示，别猜。"""
    from . import repository as repo

    target = str(name or "").strip()
    if not target:
        return None

    product = repo.get_product_by_name(target, db_path=advisor.db_path)
    if product is not None:
        return product

    # 精确名没对上，多半是店主在卡片里改了字：写成简称（"可乐" ↔ "可口可乐"），
    # 或者顺手带了空格标点。命中规则是一方为另一方的子串。
    matches = []
    for candidate in repo.list_products(db_path=advisor.db_path):
        other = str(candidate["name"]).strip()
        if target in other or other in target:
            matches.append(candidate)
    if not matches:
        return None
    # 多个候选时取名字最长的那个，再按 product_id 兜底：
    # 结果得是确定的，不能靠数据库返回顺序撞运气。
    matches.sort(key=lambda c: (-len(str(c["name"]).strip()), c["product_id"]))
    return matches[0]


def _welcome(advisor: InventoryAdvisor) -> str:
    products = advisor.stocktake_rows()
    return (f"来啦。店里有 {len(products)} 个商品我已经记着了，"
            f"想进什么货直接问我就行 —— 「可乐该进多少」「有什么卖不动了」都行。\n\n"
            + onboarding.progress_hint(advisor.db_path))


def launch(db_path: Any = None, client: Optional[Any] = None,
           server_name: str = "127.0.0.1", server_port: int = 7860) -> None:
    # Gradio 6 的 launch() 没有 show_api 参数了；「不暴露接口」改成在
    # 每个事件上写 api_visibility="private"，见 build_demo。
    build_demo(db_path=db_path, client=client).launch(
        server_name=server_name, server_port=server_port,
    )