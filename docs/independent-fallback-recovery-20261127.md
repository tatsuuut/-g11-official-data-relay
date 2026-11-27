# 国サイト停止後の独立取得経路の復旧 — 2026-11-27

時刻はすべて日本時間。対象は当日の直前入力の取得と、その保存済み入力を使う既存の予想・配信経路。

## 確認された停止

[本番run 45976561618 / job 140430360850](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/45976561618/job/140430360850)では、`beforeinfo-20261127-07-09` の国サイトへの要求が18:51:16.846に始まり、18:51:17.056に403を受けた。その後、国サイトの共有台帳は `STOPPED` になった。

[後続run 45979393743 / job 140440074869](https://github.com/tatsuuut/-g11-official-data-relay/actions/runs/45979393743/job/140440074869)も停止状態を引き継ぎ、新しい国サイトHTTP要求は0件、新規入力も0件だった。

既存の第2・第3経路には、国サイトの `STOPPED` を取得前に読む箇所がある。ここで独立ホストへの要求まで止まり、3系統の新しい直前入力・LOCK更新につながらなかった。独立ホストから新たな403を受けたという証拠ではない。国サイトへの再要求や台帳の停止解除では、この取得経路の結合を直せない。

取得前の結合を確認した箇所は、`live_source_failover_v1.capture`、`published_preview_v1` の対象選択、`venue_market_v1.capture_due` の入口・provider loop、`shared_original_display_v1` の拒否判定。現行frozen入力実装とcandidate入力実装に同型の判定がある。

## 維持する取得順と対象ホスト

| 入力 | 既存の経路 | 許可する独立ホスト |
|---|---|---|
| 通常展示・進入/ST・水面 | 国サイト → published daily → BOATCASTの4 packet | `boatraceopenapi.github.io`、`race.boatcast.jp` |
| オリジナル展示 | BOATCAST → 対応が確認された場公式 → Fun | `race.boatcast.jp`、`www.boatrace-fukuoka.com`、`boatrace-amagasaki.jp`、`www.boatrace-tokuyama.jp`、`www.wmb.jp`、`boatrace-fun.net` |
| オッズ | 既存のvenue-market経路を維持 | `race.boatcast.jp`、対応レースの `www.omurakyotei.jp` |

展示のBOATCAST経路は `tkz`、`stt`、`sui`、`str3` の既存packet契約を使う。場公式・大村等の対応範囲は既存providerのrace-key照合に従う。未対応の場や任意URLへ対象を広げない。オッズの新しい「第3経路」は追加しない。

国サイト `www.boatrace.jp` は独立ホストの許可集合に含めない。国サイトの `STOPPED`、拒否履歴、要求数、停止理由は保存したまま扱う。

## 復旧adapterの境界

独立ホストの取得判定だけを、国サイトの停止状態から切り離す。捕捉hookは `bounded()` のcapture中に限定し、終了・例外時とも元の関数・client・openerを復元する。checkpoint、予想、日次authorityの同一性検査へ進む時点ではhookを残さない。

許可ホスト、URLと最終URL、レース日・場・R、選手名と艇番、取得時刻・提供側時刻、締切、raw bytesとSHA、保存後の読戻しを既存のprovider契約で照合する。独立経路自身の403・休止・provider lease・取得間隔・回数上限は引き続き有効。国サイトを許可ホストへ見せかける転送や、未確認のproviderを通さない。

当日のfrozen source、model、朝予想、既存LOCK、BET、NIGHTの権限は変更しない。既存の8 capture slotsと日次のnative要求台帳の制限を維持する。このslot数は、成功以外のHTTP試行まで含む一律8要求の意味ではない。

## 保存後の予想側で確認したこと

| 消費側 | 国サイト `STOPPED` による追加の予想停止判定 | 有効入力の条件 |
|---|---|---|
| 🐜 P3 | `current_runtime_v1` とnative直前予想関数には見当たらない | 朝authority、許可された展示・オッズの保存証拠、既存input compatibility、締切・入力権限の照合 |
| 🍄 剥き / V53 | native直前予想関数には見当たらない | 朝V53 graph、正式な展示入力、raw/receipt照合、当日のlogic pin、締切前LOCK |
| 🦍 ゴリ | WILDの予想・既存alternate BET入力読戻しには見当たらない | pinned verifierの `VERIFIED_ALTERNATE_LIVE_RECEIPT_EXACT_120_V2`、120通り、当日roster・締切・upstream packet等の照合 |

V53には、primary展示が無い場合に `published-exhibition` を読むnative経路がある。P3にもpublished展示とvenue-marketの組を読むnative経路がある。

ゴリのraw `runtime_orchestrator_v1` はprimary名のsnapshotを読む部分を持つ。一方、現行WILD childには、別processの `verified_wild_alternate_odds_v1` で検証したV2入力を既存の正式consumerへ渡す経路がある。古い `HELD_PENDING_INPUT_CONTRACT` の場オッズ証拠は隔離を維持する。単に任意の場オッズfileがあるだけでは正式入力にしない。

従って、有効な独立入力が保存され、既存のinput compatibilityと朝の権限が成立すれば、国サイトの停止状態そのものを予想側で解除する必要は見当たらない。取得再開、正式LOCK、公開読戻しの成功は、それぞれ別の実行証拠で確認する。

公開へのhandoffにも別の結合があった。従来の `stadium_inventory` / `timely_stadium_sources` は大村24場の保存オッズだけを新規sourceとして数えていたため、他場のBOATCASTオッズが正常保存されても初回の公開・WILD handoff条件へ届かなかった。11月27日以降は、登録済みのBOATCAST・大村providerが再検証できる新規 `venue-market` 保存を全会場で数える。元rawのhash、providerの正確なURL・source ID、朝roster、当日plan、取得窓、120通り、締切を既存の `venue_market.recheck` と照合する。ファイル名だけの受理や国サイトのsnapshotへの偽装は行わない。11月26日以前の大村限定処理は維持する。

## 検証・本番適用の記録

以下は実行結果を受けて更新する欄。未確認を復旧済みとして扱わない。

| 項目 | 状態 |
|---|---|
| 実装commit / PR | 本番適用証拠の追記待ち |
| STOPを保持した独立capture・許可外URL拒否・例外復元の試験 | 試験結果の追記待ち |
| 正式handoffの登録provider・raw改変拒否 | 実frozen BOATCAST parser/capture/recheckを含む5件成功。本番取得・LOCKの確認とは別 |
| 実frozen/candidate入力と3系統consumerの契約試験 | 全体試験結果の追記待ち |
| public CI | run・head・結果の追記待ち |
| 独立入力の当日保存・raw読戻し | race / host / capture時刻の追記待ち |
| 🐜 / 🍄 / 🦍 の締切前正式LOCK | 系統別race / LOCK時刻の追記待ち |
| 公開サーバー読戻し | race / 読戻し時刻 / 締切との差の追記待ち |

公開読戻し時刻はサーバー側の検証完了時刻。端末で実際に表示された時刻とは分けて記録する。
