"""Bilingual (Traditional Chinese / English) strings for the UNO game.

Everything user-visible goes through :func:`t` so the whole app can switch
language at runtime with one variable.  Keys are stable; values are tuples
``(zh, en)``.
"""
from __future__ import annotations

import locale

LANGS = ("zh", "en")
DEFAULT_LANG = "zh"


def detect_lang() -> str:
    """Best-effort default from the environment (zh-TW → zh)."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        import os

        value = os.environ.get(var)
        if value and value.lower().startswith(("zh", "cmn")):
            return "zh"
    try:
        loc = locale.getlocale()[0] or ""
    except Exception:  # pragma: no cover - platform quirk
        loc = ""
    return "zh" if loc.lower().startswith(("zh", "cmn")) else "en"


def _l(lang: str) -> int:
    return 0 if lang == "zh" else 1


def t(key: str, lang: str = DEFAULT_LANG, **kw: object) -> str:
    """Translate ``key``; missing keys degrade to the key itself."""
    pair = STR.get(key)
    text = pair[_l(lang)] if pair else key
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text
    return text


# --------------------------------------------------------------------------- UI
STR: dict[str, tuple[str, str]] = {
    # app / menu
    "app_title": ("UNO 區網對戰", "UNO LAN Party"),
    "app_subtitle": ("用 Python 寫的多人 UNO，區網即開即玩", "Multiplayer UNO over your local network"),
    "menu_host": ("建立房間", "Host a room"),
    "menu_host_hint": ("開房並等待朋友加入", "Open a room for friends on this network"),
    "menu_join": ("加入房間", "Join a room"),
    "menu_join_hint": ("自動搜尋區網房間，或輸入 IP", "Auto-discover rooms or type an IP"),
    "menu_lang": ("語言：中文", "Language: English"),
    "menu_quit": ("離開", "Quit"),
    "menu_help": ("操作說明", "How to play"),
    "name_label": ("你的名字", "Your name"),
    "name_default": ("玩家", "Player"),
    "tab_lan": ("區網", "LAN"),
    # lobby
    "lobby_title": ("房間大廳", "Room lobby"),
    "lobby_room": ("房間", "Room"),
    "lobby_host_label": ("你的房間已開啟", "Your room is open"),
    "lobby_join_hint": ("搜尋中… 或輸入房主 IP", "Searching… or type the host IP"),
    "lobby_search": ("搜尋區網", "Scan network"),
    "lobby_searching": ("搜尋中…", "Scanning…"),
    "lobby_no_rooms": ("沒有找到房間", "No rooms found"),
    "lobby_rooms": ("找到的房間", "Rooms found"),
    "lobby_join_btn": ("加入", "Join"),
    "lobby_spectate_btn": ("觀戰", "Watch"),
    "lobby_players": ("玩家", "Players"),
    "lobby_spectators": ("觀戰者", "Spectators"),
    "lobby_empty_seats": ("還有 {n} 個空位", "{n} seats free"),
    'chat_empty': ('還沒有人說話', 'No messages yet'),
    'log_empty': ('尚無紀錄', 'No moves yet'),
    'lobby_need_players': ('還需要 {n} 人才能開始', 'Need {n} more player(s)'),
    'lobby_room_open': ('房間已在區網公開', 'Room is on the LAN'),
    "lobby_share_hint": ("朋友在「加入房間」自動搜尋，或輸入這台電腦的 IP 與埠 {port}",
                        "Friends can auto-discover the room, or join with this PC's IP and port {port}"),
    "lobby_start": ("開始遊戲", "Start game"),
    "lobby_wait_host": ("等待房主開始…", "Waiting for the host…"),
    "lobby_kick": ("踢出", "Kick"),
    "lobby_leave": ("離開房間", "Leave room"),
    "lobby_rules": ("規則設定", "Rules"),
    "lobby_rules_hint": ("只有房主可以修改，開始後鎖定", "Host only, locked after start"),
    "lobby_seats": ("{n}/10 人", "{n}/10 players"),
    "lobby_status_lobby": ("等待中", "Waiting"),
    "lobby_status_playing": ("遊戲中", "Playing"),
    "lobby_status_round": ("回合結束", "Round over"),
    "lobby_status_match": ("比賽結束", "Match over"),
    "lobby_host_badge": ("房主", "Host"),
    "lobby_you_badge": ("你", "You"),
    "you_suffix": ("（你）", " (you)"),
    "lobby_offline_badge": ("離線", "offline"),
    "lobby_connected": ("已連線", "Connected"),
    "lobby_disconnected": ("已斷線", "Disconnected"),
    "lobby_join_ip": ("房主 IP", "Host IP"),
    "lobby_spectate_help": ("觀戰模式會看到所有人的手牌", "Spectators can see every hand"),
    # connecting / net
    "net_connecting": ("連線中…", "Connecting…"),
    "game_uno_press": ("快按 UNO！剩 {s} 秒", "Press UNO! — {s}s left"),
    "game_uno_catch": ("可以抓 UNO！剩 {s} 秒", "You can catch! — {s}s left"),
    "net_renamed": ("名稱重複，改用 {name} 加入", "Name taken — joined as {name}"),
    "net_connected": ("已連線", "Connected"),
    "net_disconnected": ("與房間的連線中斷", "Connection to the room was lost"),
    "net_reconnecting": ("重新連線中…", "Reconnecting…"),
    "net_error": ("連線錯誤：{msg}", "Connection error: {msg}"),
    "net_refused": ("無法連線，請確認 IP 與房主是否開房", "Cannot connect — check the IP and that the host is waiting"),
    "net_host_failed": ("開房失敗：{msg}", "Could not host: {msg}"),
    # game table
    "game_you": ("你", "You"),
    "game_turn_you": ("你的回合！", "Your turn!"),
    "game_turn_wait": ("等待 {name} 出牌…", "Waiting for {name}…"),
    "game_direction": ("方向", "Direction"),
    "game_direction_cw": ("順時針", "Clockwise"),
    "game_direction_ccw": ("逆時針", "Counter-clockwise"),
    "game_color": ("目前顏色", "Current colour"),
    "game_draw_pile": ("牌堆", "Draw pile"),
    "game_draw_n": ("抽牌 ({n})", "Draw ({n})"),
    "game_hand": ("你的手牌（{n}）", "Your hand ({n})"),
    "game_btn_draw": ("抽一張", "Draw"),
    "game_btn_pass": ("結束回合", "End turn"),
    "game_btn_uno": ("喊 UNO！", "UNO!"),
    "game_btn_catch": ("抓 UNO", "Catch!"),
    "game_btn_leave": ("離開", "Leave"),
    "game_btn_next_round": ("下一局", "Next round"),
    "game_btn_rematch": ("再來一場", "Rematch"),
    "game_wait_next_round": ("等待房主開始下一局…", "Waiting for the host…"),
    "game_wait_rematch": ("等待房主再來一場…", "Waiting for the host to restart…"),
    "game_pending": ("累積罰抽 +{n}", "Stacked draw +{n}"),
    "game_stack_hint": ("可以疊 {card}，或按抽牌接受處罰", "Stack a {card} or draw to take the penalty"),
    "game_color_prompt": ("選一個顏色", "Choose a colour"),
    "game_swap_prompt": ("選一位玩家交換手牌", "Swap hands with…"),
    "game_swap_cancel": ("取消（超過 {n} 秒自動選最少牌的人）", "Cancel (auto-picks after {n}s)"),
    "game_choosing": ("{name} 正在選交換對象…", "{name} is choosing who to swap with…"),
    "game_uno_window": ("{name} 剩一張牌！快抓！", "{name} is on one card — catch them!"),
    "game_spectating": ("觀戰中", "Spectating"),
    "game_spectator_hint": ("你看得到所有人的手牌", "You can see everyone's hands"),
    "game_round_no": ("第 {n} 局", "Round {n}"),
    "game_target": ("目標 {n} 分", "First to {n}"),
    "game_pile": ("剩 {n} 張", "{n} left"),
    "game_cards_n": ("{n} 張", "{n} cards"),
    "game_cards_short": ("牌", "Cards"),
    "game_score_short": ("分", "Pts"),
    "game_drag_release": ("放開即可出牌", "Release to play"),
    "game_drag_jump": ("放開搶牌！", "Release to jump in!"),
    "game_drag_illegal": ("這張不能出", "That card can't be played"),
    "game_click_to_play": ("把牌拖到桌面出牌", "Drag a card up to play"),
    "game_not_your_turn": ("還沒輪到你", "Not your turn"),
    "game_must_draw": ("請先抽牌", "Draw first"),
    "game_drawn_hint": ("抽到 {card}，拖到桌面打出或按結束回合", "Drew {card} — drag it up or end your turn"),
    "game_rank": ("排名", "Rank"),
    "game_score": ("分數", "Score"),
    "game_wins": ("勝場", "Wins"),
    "game_round_over": ("第 {n} 局結束", "Round {n} over"),
    "game_round_winner": ("{name} 贏了這局", "{name} wins the round"),
    "game_round_hand": ("{name}：{n} 分", "{name}: {n} pts"),
    "game_match_over": ("比賽結束", "Match over"),
    "game_match_winner": ("{name} 獲勝！", "{name} wins the match!"),
    "game_wins_n": ("{n} 勝", "{n} wins"),
    "game_round_points": ("這局得分", "Round points"),
    "game_reveal": ("攤牌", "Showdown"),
    "game_left_room": ("你已離開房間", "You left the room"),
    "game_host_left": ("房主離開了房間", "The host left the room"),
    "game_kicked": ("你被房主移出房間", "You were removed by the host"),
    "game_you_are_spectator": ("你正在觀戰", "You are spectating"),
    # chat / log
    "chat_title": ("聊天室", "Chat"),
    "react_leader_only": ("🔒 領先的玩家專用", "🔒 Leader only"),
    "react_leader_you": ("你正在領先！", "You are leading!"),
    "react_locked": ("只有目前領先的玩家能用這個表情", "Only the current leader can use this reaction"),
    "react_spectator": ("旁觀者不能送表情", "Spectators can't send reactions"),
    "chat_send": ("送出", "Send"),
    "chat_placeholder": ("輸入訊息…", "Type a message…"),
    "log_title": ("紀錄", "Log"),
    "log_round_start": ("第 {n} 局開始，{name} 先手", "Round {n} starts — {name} goes first"),
    "log_play": ("{name} 打出 {card}", "{name} played {card}"),
    "log_play_pick": ("{name} 打出 {card}，指定 {color}", "{name} played {card} and chose {color}"),
    "log_draw": ("{name} 抽了 {n} 張", "{name} drew {n}"),
    "log_draw_one": ("{name} 抽了一張", "{name} drew a card"),
    "log_skip": ("{name} 被跳過", "{name} was skipped"),
    "log_reverse": ("方向反轉", "Direction reversed"),
    "log_pending": ("罰抽累積到 +{n}", "Draw penalty is now +{n}"),
    "log_turn": ("輪到 {name}", "{name}'s turn"),
    "log_uno": ("{name} 喊了 UNO！", "{name} called UNO!"),
    "log_uno_safe": ("{name} 安全下莊", "{name} survived the UNO window"),
    "log_catch": ("{name} 抓到 {target} 沒喊 UNO，罰抽 {n} 張", "{name} caught {target} without UNO — +{n} cards"),
    "log_swap": ("{name} 和 {target} 交換手牌", "{name} swapped hands with {target}"),
    "log_rotate": ("所有手牌往下家傳", "Everyone passed their hand on"),
    "log_round_end": ("{name} 拿下這局（+{n} 分）", "{name} takes the round (+{n} pts)"),
    "log_match_end": ("{name} 達到目標分數，贏得比賽！", "{name} reached the target and wins the match!"),
    "log_pass": ("{name} 結束回合", "{name} ended their turn"),
    "log_timeout": ("{name} 超時，自動抽牌並跳過", "{name} ran out of time — drew a card and passed"),
    "log_spectator_join": ("{name} 加入觀戰", "{name} is now spectating"),
    "log_player_join": ("{name} 加入房間", "{name} joined the room"),
    "log_player_leave": ("{name} 離開房間", "{name} left the room"),
    "log_reconnect": ("{name} 重新連線", "{name} reconnected"),
    "log_rematch": ("再來一場！", "Rematch!"),
    "log_next_round": ("下一局開始", "Next round"),
    # rules
    "rule_classic": ("經典規則", "Classic rules"),
    "rule_stacking": ("疊牌派對", "Stacking party"),
    "rule_scoring": ("計分制", "Scoring"),
    "rule_party": ("派對規則", "Party rules"),
    "rule_start_cards": ("起始手牌", "Starting cards"),
    "rule_target_score": ("目標分數", "Target score"),
    "rule_stack_draw2": ("+2 疊 +2", "+2 on +2"),
    "rule_stack_wild4": ("+4 疊 +4", "+4 on +4"),
    "rule_jump_in": ("搶牌 Jump-in", "Jump-in"),
    "rule_seven_swap": ("7 換手牌", "7 swaps"),
    "rule_zero_rotate": ("0 輪轉手牌", "0 rotates"),
    "rule_uno_window": ("UNO 反應時間（秒）", "UNO window (s)"),
    "rule_uno_penalty": ("沒喊 UNO 罰抽", "Missed-UNO penalty"),
    "rule_draw_playable": ("抽到即可出牌", "Draw & play"),
    "rule_wild4_needs_no_color": ("+4 需無同色", "+4 needs no colour"),
    "rule_turn_time": ("回合限時", "Turn timer"),
    "rule_combo_same": ("同數字連出", "Same-number combo"),
    "rule_combo_short": ("連出", "Combos"),
    "combo_btn": ("連出 ×{n}", "Play ×{n}"),
    "turn_time_off": ("關閉", "Off"),
    "rule_choice_timeout": ("換牌選擇時限（秒）", "Swap choice time (s)"),
    "rule_on": ("開", "On"),
    "rule_off": ("關", "Off"),
    "rule_points_number": ("數字牌 = 面值", "Number = face value"),
    "rule_points_action": ("功能牌 = 20 分", "Action = 20 pts"),
    "rule_points_wild": ("萬用牌 = 50 分", "Wild = 50 pts"),
    "rule_jump_hint": ("手上有和檯面完全相同的牌，可在別人回合搶出", "Play an identical card out of turn"),
    "rule_seven_hint": ("出 7 選一位玩家換手牌；出 0 所有人手牌往下家傳", "7 swaps hands, 0 passes every hand on"),
    # how-to
    "help_title": ("怎麼玩", "How to play"),
    "help_body": (
        "把牌拖到桌面上即可出牌；出萬用牌時再選顏色。\n"
        "抽牌：輪到你時按「抽一張」，抽到可出的牌可以馬上打出，否則按「結束回合」。\n"
        "剩一張牌時記得按「喊 UNO！」，否則別人可以「抓 UNO」，你會罰抽 2 張。\n"
        "7-0 規則：出 7 選一位玩家交換手牌，出 0 所有人手牌往下家傳。\n"
        "搶牌：手上有和檯面完全相同的牌，可以在別人的回合直接打出。\n"
        "計分：數字牌面值、功能牌 20 分、萬用牌 50 分，先到目標分數者獲勝。",
        "Drag a card up onto the table to play it. Wild cards ask for a colour.\n"
        "Draw: press Draw on your turn. If the drawn card is playable you may play it, otherwise End turn.\n"
        "On one card, press UNO! — otherwise someone can Catch you and you draw 2.\n"
        "7-0: a 7 swaps hands with a player, a 0 passes every hand to the next player.\n"
        "Jump-in: an identical card may be played out of turn.\n"
        "Scoring: numbers = face value, actions = 20, wilds = 50 — first to the target wins.",
    ),
    "ok": ("好", "OK"),
    "cancel": ("取消", "Cancel"),
    "close": ("關閉", "Close"),
    "back": ("返回", "Back"),
    "you": ("你", "You"),
    "unnamed_room": ("UNO 房間", "UNO room"),
    "room_title": ("{name} 的房間", "{name}'s room"),
    "players_n": ("{n} 位玩家", "{n} players"),
    "spectators_n": ("{n} 位觀戰", "{n} watching"),
    "lang_toggle": ("English", "中文"),
    "loading": ("載入中…", "Loading…"),
}

# ------------------------------------------------------------------- card names
COLOR_NAMES: dict[str, tuple[str, str]] = {
    "R": ("紅", "Red"),
    "Y": ("黃", "Yellow"),
    "G": ("綠", "Green"),
    "B": ("藍", "Blue"),
}
VALUE_NAMES: dict[str, tuple[str, str]] = {
    "skip": ("禁止", "Skip"),
    "reverse": ("反轉", "Reverse"),
    "draw2": ("+2", "+2"),
    "wild": ("萬用", "Wild"),
    "wild4": ("萬用+4", "Wild +4"),
}


def color_text(color: str | None, lang: str = DEFAULT_LANG) -> str:
    if not color:
        return "—"
    pair = COLOR_NAMES.get(color)
    return pair[_l(lang)] if pair else str(color)


def card_text(card: str | None, lang: str = DEFAULT_LANG) -> str:
    """``"R5"`` → ``紅5`` / ``Red 5``; ``"wild4"`` → ``萬用+4``."""
    if not card:
        return "—"
    if card in ("wild", "wild4"):
        return VALUE_NAMES[card][_l(lang)]
    color, value = card[0], card[1:]
    if value in VALUE_NAMES:
        return f"{color_text(color, lang)}{VALUE_NAMES[value][_l(lang)]}"
    return f"{color_text(color, lang)}{value}"


# --------------------------------------------------------------------- errors
ERRORS: dict[str, tuple[str, str]] = {
    "not_your_turn": ("還沒輪到你", "Not your turn"),
    "combo_off": ("房規沒有開啟連出", "Combo play is off"),
    "bad_combo": ("連出只能打數字相同的數字牌（出牌後的狀態不能連出）", "Combos need number cards of the same value (not after drawing or while a draw is pending)"),
    "react_locked": ("只有目前領先的玩家能用這個表情", "Only the current leader can use this reaction"),
    "not_in_hand": ("這張牌不在你的手牌裡", "That card is not in your hand"),
    "must_stack_pending": ("必須先疊牌或抽牌", "Stack a card or draw first"),
    "wild4_has_color": ("手上還有目前顏色的牌，不能出 +4", "You still hold the current colour — no +4"),
    "need_color": ("請先選一個顏色", "Pick a colour first"),
    "drawn_only": ("只能打出剛抽到的那張牌", "You may only play the card you drew"),
    "bad_jump": ("這張牌不能搶出", "You cannot jump in with that card"),
    "pick_swap": ("請先選擇交換對象", "Choose who to swap with first"),
    "illegal": ("這張牌不能出", "That card cannot be played"),
    "cannot_pass": ("現在不能結束回合", "You cannot end your turn now"),
    "must_play_or_pass": ("請打出剛抽的牌或結束回合", "Play the drawn card or end your turn"),
    "not_choosing": ("現在不需要選對象", "Nothing to choose right now"),
    "bad_target": ("交換對象無效", "Invalid swap target"),
    "not_playing": ("遊戲尚未開始", "The game is not running"),
    "bad_player": ("玩家不存在", "Unknown player"),
    "not_round_over": ("這一局還沒結束", "The round is still running"),
    "lobby_full": ("房間已滿（最多 10 人）", "The room is full (10 players)"),
    "spectator_full": ("觀戰人數已滿", "No spectator slots left"),
    "name_taken": ("這個名字有人用了", "That name is taken"),
    "not_host": ("只有房主可以這麼做", "Only the host can do that"),
    "not_lobby": ("遊戲已開始，無法變更", "The game already started"),
    "need_players": ("至少需要 2 位玩家", "At least 2 players are needed"),
    "seat_taken": ("座位已被佔用", "That seat is taken"),
    "not_a_player": ("你不是這局的玩家", "You are not a player in this game"),
    "unknown_type": ("未知的指令", "Unknown command"),
    "rate_limited": ("操作太快了，等一下", "Slow down a little"),
    "bad_msg": ("指令格式錯誤", "Malformed message"),
    "bad_version": ("版本不相容，請兩邊都用同一版程式", "Version mismatch — use the same build on both sides"),
    "kicked": ("你被移出房間", "You were removed from the room"),
}


def error_text(code: str, lang: str = DEFAULT_LANG) -> str:
    pair = ERRORS.get(code)
    return pair[_l(lang)] if pair else code


def rules_text(rules: dict, lang: str = DEFAULT_LANG) -> str:
    """One-line summary of the enabled rule set."""
    bits = [t("rule_classic", lang)]
    if rules.get("stack_draw2") or rules.get("stack_wild4"):
        bits.append(t("rule_stacking", lang))
    if rules.get("jump_in") or rules.get("seven_swap") or rules.get("zero_rotate"):
        bits.append(t("rule_party", lang))
    if rules.get("target_score"):
        bits.append(t("game_target", lang, n=rules["target_score"]))
    if rules.get("combo_same"):
        bits.append(t("rule_combo_short", lang))
    if rules.get("turn_time"):
        bits.append(f"⏱ {int(rules['turn_time'])}s")
    return " · ".join(dict.fromkeys(bits))
