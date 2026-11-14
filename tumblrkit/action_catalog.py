"""Stable 79-action vocabulary restored from tumblr-chat-ux-catalog.zip.

The catalog is navigation/design metadata, NOT evidence that an external
provider or one of these operations is implemented. Only existing approved
adapter actions may execute. No seventh top-level goal or 79-button UI.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogAction:
    action_id: str
    domain: str
    name: str
    confirmation: bool
    output_type: str


# Exact order and names from the 2026-10-30 source artifact.
AREAS = (
    ("account", "接続・設定", (
        "接続状態を見る", "接続を開始", "再認証する",
        "操作権限を見る", "設定を確認する", "接続を解除する"
    ), (2,3,6), "report-card"),
    ("discovery", "巡回・発見", (
        "新着を巡回", "フォロワーを確認", "フォロー先を巡回",
        "新しいブログを探す", "タグから探す", "対象を登録・解除", "差分を見る"
    ), (), "media-list"),
    ("library", "閲覧・履歴", (
        "投稿一覧を見る", "画像・動画を開く", "既読にする", "お気に入りに登録",
        "あとで見るに登録", "前回の続きから見る", "検索する", "登録を解除"
    ), (), "media-list"),
    ("media", "メディア保存", (
        "保存候補を見る", "動画をMP4で保存", "画像を保存", "保存状況を見る",
        "保存失敗を再試行", "保存済みを探す", "容量を確認"
    ), (), "media-list"),
    ("engagement", "交流・反応", (
        "スキを付ける", "リブログ下書きを作る", "リブログする", "フォロー候補を見る",
        "フォローする", "交流履歴を見る", "操作を取り消す"
    ), (3,5), "report-card"),
    ("publisher", "投稿・宣伝", (
        "投稿文をゼロから作る", "元文を手直しする", "過去文から作り直す",
        "画像・動画を添付", "タグを提案", "プレビューする", "今すぐ投稿",
        "予約投稿", "予約を変更", "投稿を編集", "投稿を削除", "投稿結果を見る"
    ), (7,8,9,11), "report-card"),
    ("campaign", "キャンペーン", (
        "新規企画を作る", "企画文を作る", "応募条件を設定", "応募状況を見る",
        "重複・無効候補を見る", "応募締切を変更", "抽選を実行",
        "当選者へ通知", "期限切れを確認", "繰り上げを行う",
        "企画を終了", "企画結果を見る"
    ), (6,7,8,10,11), "report-card"),
    ("referral", "紹介・拡散", (
        "紹介リンクを発行", "ワンタッチ共有文を作る", "リンクを共有",
        "紹介キャンペーンを設定", "紹介成立を確認", "段階特典を設定",
        "特典達成を確認", "付与履歴を見る", "紹介リンクを停止"
    ), (3,6,7), "report-card"),
    ("analytics", "集計・分析", (
        "今日の集計", "今週の集計", "今月の集計", "期間比較", "フォロワー推移",
        "投稿別反応", "キャンペーン成果", "紹介成果", "Telegram流入経路",
        "未分類流入を見る", "レポートを出す"
    ), (), "report-card"),
)


def catalog():
    return tuple(
        CatalogAction(f"{domain}.{n:02d}",domain,name,n in confirmations,
                      "preview-and-confirm" if n in confirmations else output)
        for domain, _, names, confirmations, output in AREAS
        for n,name in enumerate(names,1)
    )


CATALOG = catalog()
BY_ID = {action.action_id:action for action in CATALOG}


def lookup(action_id):
    if not isinstance(action_id,str):
        raise ValueError("catalog_action_invalid")
    item=BY_ID.get(action_id)
    if item is None:
        raise ValueError("catalog_action_unlisted")
    return item
