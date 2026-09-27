import streamlit as st
import google.generativeai as genai
import pandas as pd
from PIL import Image
import io
import requests
import base64
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os
import difflib

# 画像検索用ツール（背景切り抜きはサーバー負荷のため一時除外）
from duckduckgo_search import DDGS

Image.MAX_IMAGE_PIXELS = None 

# ==========================================
# 1. 初期設定
# ==========================================
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
IMGBB_API_KEY = st.secrets["IMGBB_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-3.6-flash')

# ==========================================
# 2. スプレッドシート接続＆画像クラウド保存処理
# ==========================================
def connect_to_spreadsheet():
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

def optimize_image_in_memory(uploaded_file):
    img = Image.open(uploaded_file)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    img.thumbnail((1024, 1024))
    return img

def save_compressed_image(optimized_image, product_id):
    try:
        img_byte_arr = io.BytesIO()
        optimized_image.save(img_byte_arr, format='JPEG', quality=70)
        img_byte_arr.seek(0)
        encoded_image = base64.b64encode(img_byte_arr.read()).decode('utf-8')
        
        url = "https://api.imgbb.com/1/upload"
        payload = {
            "key": IMGBB_API_KEY,
            "image": encoded_image,
            "name": product_id
        }
        response = requests.post(url, data=payload)
        
        if response.status_code == 200:
            result = response.json()
            return result['data']['url']
        else:
            raise Exception(f"ImgBBエラー: {response.text}")
    except Exception as e:
        st.error(f"画像のアップロードに失敗しました: {e}")
        return ""

# ==========================================
# 3. マスターデータ処理関数
# ==========================================
def load_store_master():
    try:
        sh = connect_to_spreadsheet()
        store_sheet = sh.worksheet("販売店マスター")
        store_data = store_sheet.get_all_records()
        if not store_data: return ["選択してください"]
        return ["選択してください"] + [row["販売店名"] for row in store_data]
    except Exception:
        return ["選択してください"]

def add_new_store(store_name, area_name):
    try:
        sh = connect_to_spreadsheet()
        store_sheet = sh.worksheet("販売店マスター")
        current_rows = len(store_sheet.get_all_values())
        new_id = f"S{current_rows:03d}"
        store_sheet.append_row([new_id, store_name, area_name])
        return True
    except Exception as e:
        st.error(f"店舗の追加に失敗しました: {e}")
        return False

def load_category_master():
    sh = connect_to_spreadsheet()
    try:
        cat_sheet = sh.worksheet("カテゴリーマスター")
    except gspread.exceptions.WorksheetNotFound:
        cat_sheet = sh.add_worksheet(title="カテゴリーマスター", rows="100", cols="4")
        cat_sheet.append_row(["カテゴリー名", "規格1ラベル", "規格2ラベル", "規格3ラベル"])
    cat_data = cat_sheet.get_all_records()
    cat_dict = {}
    for row in cat_data:
        cat_dict[row["カテゴリー名"]] = [row.get("規格1ラベル", "規格1"), row.get("規格2ラベル", "規格2"), row.get("規格3ラベル", "規格3")]
    return cat_dict

def add_new_category(cat_name, label1, label2, label3):
    try:
        sh = connect_to_spreadsheet()
        cat_sheet = sh.worksheet("カテゴリーマスター")
        cat_sheet.append_row([cat_name, label1, label2, label3])
        return True
    except Exception as e:
        st.error(f"カテゴリー追加に失敗しました: {e}")
        return False

def load_product_master():
    try:
        sh = connect_to_spreadsheet()
        prod_sheet = sh.worksheet("商品マスター")
        all_values = prod_sheet.get_all_values()
        if len(all_values) <= 1: return {}
        headers = all_values[0]
        prod_dict = {}
        for i, row in enumerate(all_values[1:], start=2):
            if row and len(row) > 0 and row[0]:
                padded_row = row + [""] * (len(headers) - len(row))
                row_data = {headers[j]: padded_row[j] for j in range(len(headers))}
                row_data['_row_idx'] = i 
                key = f"[{row_data['商品ID']}] {row_data.get('商品名', '不明')}"
                prod_dict[key] = row_data
        return prod_dict
    except Exception:
        return {}

def get_top_candidates(ai_name, prod_master, top_n=3):
    if not ai_name or not prod_master: return []
    scores = []
    for key, data in prod_master.items():
        score = difflib.SequenceMatcher(None, ai_name, data.get('商品名', '')).ratio()
        scores.append((score, key))
    scores.sort(reverse=True, key=lambda x: x[0])
    return [item[1] for item in scores[:top_n] if item[0] > 0.15]

def save_or_update_price(store_name, prod_id, prod_name, price_notax, price_tax, user_name):
    sh = connect_to_spreadsheet()
    price_sheet = sh.worksheet("店舗別価格表")
    today = datetime.now().strftime("%Y/%m/%d")

    headers = price_sheet.row_values(1)
    required = ["販売店名", "商品ID", "商品名", "現行売価(税抜)", "現行売価(税込)", "更新日", "担当者", "過去売価1(税抜)", "過去売価1(税込)", "過去日1", "過去売価2(税抜)", "過去売価2(税込)", "過去日2"]
    if len(headers) < len(required):
        for i, h in enumerate(required):
            if i >= len(headers): price_sheet.update_cell(1, i+1, h)

    all_values = price_sheet.get_all_values()
    found_idx = -1
    for i, row in enumerate(all_values):
        if i == 0: continue
        if len(row) >= 2 and row[0] == store_name and row[1] == prod_id:
            found_idx = i + 1
            break

    in_notax, in_tax = str(price_notax), str(price_tax)

    if found_idx != -1:
        row = all_values[found_idx - 1]
        padded_row = row + [""] * (13 - len(row))
        curr_notax, curr_tax, curr_date = str(padded_row[3]), str(padded_row[4]), padded_row[5]
        hist1_notax, hist1_tax, hist1_date = padded_row[7], padded_row[8], padded_row[9]

        if curr_notax == in_notax and curr_tax == in_tax:
            price_sheet.update(f'F{found_idx}:G{found_idx}', [[today, user_name]])
        else:
            new_values = [in_notax, in_tax, today, user_name, curr_notax, curr_tax, curr_date, hist1_notax, hist1_tax, hist1_date]
            price_sheet.update(f'D{found_idx}:M{found_idx}', [new_values])
    else:
        price_sheet.append_row([store_name, prod_id, prod_name, in_notax, in_tax, today, user_name, "", "", "", "", "", ""])

# ==========================================
# 4. 画面ごとの関数
# ==========================================
def authenticate_user(username, password):
    try:
        sh = connect_to_spreadsheet()
        users = sh.worksheet("ユーザー管理").get_all_records()
        for user in users:
            if str(user.get("ユーザーID", "")) == username and str(user.get("パスワード", "")) == password:
                return True, user.get("氏名", "不明"), str(user.get("PINコード", ""))
        return False, "", ""
    except Exception as e:
        return False, "", ""

def login_screen():
    st.title("🔐 システムログイン")
    if 'auth_stage' not in st.session_state: st.session_state['auth_stage'] = 1
        
    if st.session_state['auth_stage'] == 1:
        st.write("支給されたIDとパスワードを入力してください。")
        username = st.text_input("ユーザーID")
        password = st.text_input("パスワード", type="password")
        if st.button("次へ", type="primary", use_container_width=True):
            with st.spinner("認証中..."):
                is_auth, name, expected_pin = authenticate_user(username, password)
                if is_auth:
                    st.session_state['temp_name'], st.session_state['expected_pin'], st.session_state['auth_stage'] = name, expected_pin, 2
                    st.rerun()
                else:
                    st.error("ユーザーIDかパスワードが間違っています。")
                    
    elif st.session_state['auth_stage'] == 2:
        st.write(f"👤 **{st.session_state['temp_name']}** さん")
        entered_pin = st.text_input("二段階認証：4桁のPINコード", type="password", placeholder="****")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("戻る", use_container_width=True):
                st.session_state['auth_stage'] = 1
                st.rerun()
        with col2:
            if st.button("認証する", type="primary", use_container_width=True):
                if not st.session_state['expected_pin']:
                    st.error("スプレッドシートにPINコードが設定されていません。")
                elif entered_pin == st.session_state['expected_pin']:
                    st.session_state['logged_in'], st.session_state['user_name'], st.session_state['auth_stage'] = True, st.session_state['temp_name'], 1 
                    st.rerun()
                else:
                    st.error("PINコードが間違っています。")

# ---------------------------------------------
# 画面①：店舗巡回（価格チェック）
# ---------------------------------------------
def price_check_app():
    st.title("🛒 店舗巡回＆価格チェック")
    if 'entry_mode' not in st.session_state: st.session_state.entry_mode = None
    if 'selected_store_val' not in st.session_state: st.session_state.selected_store_val = "選択してください"

    if st.session_state.entry_mode is None:
        st.write("### 1. 販売店の選択")
        STORE_MASTER = load_store_master()
        try: idx = STORE_MASTER.index(st.session_state.selected_store_val)
        except ValueError: idx = 0
            
        st.session_state.selected_store_val = st.selectbox("販売店を選択", STORE_MASTER, index=idx)
        with st.expander("➕ 新しい販売店を追加する場合はこちら"):
            new_store_name, new_area_name = st.text_input("販売店名"), st.text_input("エリア")
            if st.button("マスターに登録") and new_store_name:
                with st.spinner("登録中..."):
                    if add_new_store(new_store_name, new_area_name):
                        st.success(f"「{new_store_name}」を登録しました！")
                        st.rerun()

        st.markdown("---")
        st.write("### 2. 商品の登録方法を選択")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("📸 画像で一括判別\n\n(AIが自動読み取り)", type="primary", use_container_width=True):
                if st.session_state.selected_store_val == "選択してください": st.error("先に販売店を選択してください。")
                else: st.session_state.entry_mode = 'image'; st.rerun()
        with col2:
            if st.button("🔍 アイテム名で検索\n\n(手動で探して登録)", type="primary", use_container_width=True):
                if st.session_state.selected_store_val == "選択してください": st.error("先に販売店を選択してください。")
                else: st.session_state.entry_mode = 'manual'; st.rerun()

    elif st.session_state.entry_mode == 'image':
        if st.button("⬅️ 店舗・入力方法の選択に戻る"):
            st.session_state.entry_mode = None
            if 'scanned_data' in st.session_state: del st.session_state['scanned_data']
            st.rerun()
            
        st.info(f"🏢 **対象店舗:** {st.session_state.selected_store_val}")
        uploaded_file = st.file_uploader("棚の画像を撮影 / 選択", type=["jpg", "jpeg", "png"])
        
        if uploaded_file is not None:
            image = optimize_image_in_memory(uploaded_file)
            st.session_state['uploaded_image'] = image # ★元画像をセッションに保存して登録画面で使えるようにする
            st.image(image, caption="アップロード画像", use_container_width=True)

            if st.button("🤖 AIで商品を解析する"):
                with st.spinner("AIが解析中です..."):
                    try:
                        prompt = 'この画像に写っている全ての商品と売価を読み取ってください。以下のJSONのみを出力してください。\n[{"カテゴリー": "トイレットペーパー", "商品名": "商品A", "価格": 298, "税区分": "税別"}]'
                        response = model.generate_content([prompt, image])
                        st.session_state['scanned_data'] = json.loads(response.text.strip().replace("```json", "").replace("```", ""))
                        st.success("読み取り完了！")
                    except Exception as e:
                        st.error(f"エラーが発生しました: {e}")

        if 'scanned_data' in st.session_state:
            st.markdown("---")
            PROD_MASTER = load_product_master()
            all_options = ["選択してください (マスター該当なし)"] + list(PROD_MASTER.keys())
            
            for i, item in enumerate(st.session_state['scanned_data']):
                ai_name, ai_price, ai_tax = item.get('商品名', '不明'), int(item.get('価格', 0)), item.get('税区分', '税別')
                state_key = f"selected_master_{i}"
                candidates = get_top_candidates(ai_name, PROD_MASTER)
                if state_key not in st.session_state: st.session_state[state_key] = candidates[0] if candidates else all_options[0]

                with st.container(border=True):
                    st.markdown(f"### 🔍 AI読取: `{ai_name}`")
                    if candidates:
                        cand_cols = st.columns(3) # ★必ず3つ一列の枠を作る
                        for c_idx, cand_key in enumerate(candidates[:3]):
                            with cand_cols[c_idx]:
                                cand_img = PROD_MASTER[cand_key].get('商品画像URL', '')
                                if cand_img and cand_img.startswith("http"): st.image(cand_img, use_container_width=True)
                                else: st.info("画像なし")
                                
                                st.caption(PROD_MASTER[cand_key].get('商品名', '不明')) # ★商品名も小さく表示
                                if st.button("👆 選択", key=f"btn_{i}_{c_idx}", use_container_width=True):
                                    st.session_state[state_key] = cand_key; st.rerun()
                    
                    st.markdown("---")
                    col_img, col_form = st.columns([1, 1])
                    with col_img:
                        selected_master = st.session_state.get(state_key, all_options[0])
                        if selected_master != all_options[0]:
                            img_path = PROD_MASTER[selected_master].get('商品画像URL', '')
                            if img_path and img_path.startswith("http"): st.image(img_path, use_container_width=True)
                            else: st.info("📷 画像未登録")
                        else: st.warning("⚠️ マスター未選択")
                    with col_form:
                        if st.session_state[state_key] not in all_options: st.session_state[state_key] = all_options[0]
                        st.selectbox("🔗 紐付けマスター", options=all_options, key=state_key)
                        st.number_input("💰 売価", value=ai_price, step=1, key=f"price_{i}")
                        st.selectbox("🧾 税区分", ["税別", "税込"], index=0 if ai_tax == '税別' else 1, key=f"tax_{i}")
                        
                        st.markdown("<br>", unsafe_allow_html=True)
                        if st.button("➕ これ以外の為、商品マスターに登録", key=f"new_master_{i}", type="secondary"):
                            st.session_state['new_prod_init_name'] = ai_name
                            st.session_state['new_prod_init_price'] = st.session_state[f"price_{i}"]
                            st.session_state['new_prod_init_tax'] = st.session_state[f"tax_{i}"]
                            st.session_state['return_to_image_mode'] = True
                            st.session_state.entry_mode = 'new_product_from_field'
                            st.rerun()
                            
            if st.button("💾 全て確認してスプレッドシートに登録", type="primary", use_container_width=True):
                with st.spinner("保存中..."):
                    try:
                        for i, item in enumerate(st.session_state['scanned_data']):
                            final_master = st.session_state[f"selected_master_{i}"]
                            if final_master != all_options[0]:
                                save_or_update_price(
                                    st.session_state.selected_store_val, PROD_MASTER[final_master]['商品ID'], PROD_MASTER[final_master].get('商品名', ''),
                                    st.session_state[f"price_{i}"] if st.session_state[f"tax_{i}"] == '税別' else "",
                                    st.session_state[f"price_{i}"] if st.session_state[f"tax_{i}"] == '税込' else "",
                                    st.session_state.get('user_name', '不明')
                                )
                        st.success("✅ スプレッドシートへの登録・更新が完了しました！")
                        del st.session_state['scanned_data']
                        st.rerun()
                    except Exception as e: st.error(f"保存に失敗しました: {e}")

    elif st.session_state.entry_mode == 'manual':
        col_back, col_new = st.columns([1, 1])
        with col_back:
            if st.button("⬅️ メニューに戻る", use_container_width=True):
                st.session_state.entry_mode = None
                st.rerun()
        with col_new:
            if st.button("➕ 新しい商品をマスター登録する", type="primary", use_container_width=True):
                st.session_state.entry_mode = 'new_product_from_field'
                st.rerun()
            
        st.info(f"🏢 **対象店舗:** {st.session_state.selected_store_val}")
        PROD_MASTER = load_product_master()
        if not PROD_MASTER:
            st.warning("商品マスターが登録されていません。")
        else:
            with st.expander("▼ 検索フィルターを開く", expanded=True):
                prod_data = list(PROD_MASTER.values())
                c_list = ["すべて"] + list(set([str(r.get('カテゴリー', '')) for r in prod_data if r.get('カテゴリー')]))
                m_list = ["すべて"] + list(set([str(r.get('メーカー名', '')) for r in prod_data if r.get('メーカー名')]))
                col_s1, col_s2, col_s3 = st.columns(3)
                with col_s1: search_cat = st.selectbox("カテゴリー", c_list)
                with col_s2: search_maker = st.selectbox("メーカー", m_list)
                with col_s3: search_word = st.text_input("商品名（キーワード）")
                
            filtered_data = [d for d in prod_data if (search_cat == "すべて" or str(d.get('カテゴリー')) == search_cat) and (search_maker == "すべて" or str(d.get('メーカー名')) == search_maker) and (search_word in str(d.get('商品名', '')))]
            prod_dict = {f"[{row['商品ID']}] {row.get('商品名', '')}": row for row in filtered_data}
            
            st.markdown("---")
            if not prod_dict: st.warning("条件に一致する商品がありません。")
            else:
                selected_prod_key = st.selectbox("登録する商品を選択", ["選択してください"] + list(prod_dict.keys()))
                if selected_prod_key != "選択してください":
                    target_prod = prod_dict[selected_prod_key]
                    with st.container(border=True):
                        col_img, col_form = st.columns([1, 1])
                        with col_img:
                            if target_prod.get('商品画像URL', '').startswith("http"): st.image(target_prod.get('商品画像URL', ''), use_container_width=True)
                            else: st.info("📷 画像未登録")
                        with col_form:
                            st.write(f"### {target_prod.get('商品名')}")
                            st.write(f"**カテゴリー:** {target_prod.get('カテゴリー')} | **メーカー:** {target_prod.get('メーカー名')}")
                            st.markdown("---")
                            with st.form(key="manual_price_form"):
                                man_price = st.number_input("💰 売価を入力", value=0, step=1)
                                man_tax = st.selectbox("🧾 税区分", ["税別", "税込"])
                                if st.form_submit_button("💾 スプレッドシートに登録", type="primary", use_container_width=True):
                                    if man_price <= 0: st.error("売価を入力してください。")
                                    else:
                                        with st.spinner("保存中..."):
                                            save_or_update_price(
                                                st.session_state.selected_store_val, target_prod['商品ID'], target_prod.get('商品名', ''),
                                                man_price if man_tax == '税別' else "", man_price if man_tax == '税込' else "", st.session_state.get('user_name', '不明')
                                            )
                                            st.success("✅ 登録しました！")

    elif st.session_state.entry_mode == 'new_product_from_field':
        if st.button("⬅️ 戻る"):
            if st.session_state.get('return_to_image_mode'):
                st.session_state.entry_mode = 'image'
            else:
                st.session_state.entry_mode = 'manual'
            st.rerun()
            
        st.write("### ✨ 新しい商品をマスターに登録")
        
        # ★ AI読取から来た場合、元画像を上に表示して見ながら入力できるようにする
        if st.session_state.get('return_to_image_mode') and 'uploaded_image' in st.session_state:
            with st.expander("📸 撮影した元画像を確認する（見ながら入力できます）", expanded=True):
                st.image(st.session_state['uploaded_image'], use_container_width=True)
                
        CAT_MASTER = load_category_master()
        cat_list = list(CAT_MASTER.keys()) if CAT_MASTER else ["(カテゴリーなし)"]
        
        selected_cat = st.selectbox("カテゴリーを選択", cat_list, key="f_cat_sel")
        labels = CAT_MASTER.get(selected_cat, ["規格1", "規格2", "規格3"])
        
        sh = connect_to_spreadsheet()
        prod_sheet = sh.worksheet("商品マスター")
        max_id = 0
        for row in prod_sheet.get_all_records():
            if str(row.get('商品ID', '')).startswith('P'):
                try: max_id = max(max_id, int(str(row['商品ID'])[1:]))
                except: pass
        next_id = f"P{max_id + 1:03d}"
        
        with st.container(border=True):
            st.info(f"自動割り当てID: **{next_id}**")
            
            init_name = st.session_state.get('new_prod_init_name', '')
            init_price = st.session_state.get('new_prod_init_price', 0)
            init_tax = st.session_state.get('new_prod_init_tax', '税別')
            
            new_name = st.text_input("商品名 *必須", value=init_name, key="f_name")
            new_maker = st.text_input("メーカー名", key="f_maker")
            col1, col2, col3 = st.columns(3)
            with col1: new_spec1 = st.text_input(labels[0], key="f_s1")
            with col2: new_spec2 = st.text_input(labels[1], key="f_s2")
            with col3: new_spec3 = st.text_input(labels[2], key="f_s3")
            new_origin = st.text_input("原産国", key="f_origin")
            
            st.markdown("---")
            st.write(f"**【価格の登録】** ※店舗 `{st.session_state.selected_store_val}` に価格も同時登録します")
            new_price = st.number_input("💰 売価", value=init_price, step=1, key="f_price")
            new_tax = st.selectbox("🧾 税区分", ["税別", "税込"], index=0 if init_tax == '税別' else 1, key="f_tax")
            
            st.markdown("---")
            st.write("### 🔍 商品画像をウェブで検索")
            search_q = st.text_input("検索キーワード (メーカー名を足すと精度UP)", value=new_name, key="f_img_q")
            if st.button("ウェブから画像を検索", key="f_search_btn"):
                with st.spinner("検索中..."):
                    try:
                        st.session_state['f_web_res'] = DDGS().images(search_q, max_results=3)
                    except Exception:
                        st.error("検索エラー。少し時間をおいて再度お試しください。")
            
            if 'f_web_res' in st.session_state and st.session_state['f_web_res']:
                st.write("候補画像 (クリックで選択):")
                cols = st.columns(3) # ★ここも必ず3つ一列の枠を作る
                for i, res in enumerate(st.session_state['f_web_res'][:3]):
                    with cols[i]:
                        st.image(res['image'], use_container_width=True)
                        if st.button("👆 選択", key=f"f_sel_{i}", use_container_width=True):
                            with st.spinner("画像を取得中..."):
                                try:
                                    img_resp = requests.get(res['image'], timeout=10)
                                    st.session_state['f_final_img'] = img_resp.content
                                    st.success("画像を取得しました！")
                                except Exception:
                                    st.error("画像の取得に失敗しました。")
            
            if 'f_final_img' in st.session_state:
                st.image(st.session_state['f_final_img'], caption="✓ 登録予定の画像", width=200)
                
            st.write("▼ または手動で画像をアップロード")
            new_img_file = st.file_uploader("手元の画像をアップロード", type=["jpg", "jpeg", "png"])
            
            if st.button("💾 マスターと価格表に登録して戻る", type="primary", use_container_width=True):
                if not new_name: st.error("商品名は必須です。")
                else:
                    with st.spinner("クラウドに保存中..."):
                        img_path = ""
                        if new_img_file:
                            optimized = optimize_image_in_memory(new_img_file)
                            img_path = save_compressed_image(optimized, next_id)
                        elif 'f_final_img' in st.session_state:
                            pil_img = Image.open(io.BytesIO(st.session_state['f_final_img']))
                            optimized = optimize_image_in_memory(pil_img)
                            img_path = save_compressed_image(optimized, next_id)
                            
                        prod_sheet.append_row([next_id, selected_cat, new_name, new_maker, new_spec1, new_spec2, new_spec3, new_origin, img_path])
                        
                        save_or_update_price(
                            st.session_state.selected_store_val, next_id, new_name,
                            new_price if new_tax == '税別' else "", new_price if new_tax == '税込' else "", st.session_state.get('user_name', '不明')
                        )
                        st.success("登録完了！")
                        
                        for k in ['new_prod_init_name', 'new_prod_init_price', 'new_prod_init_tax', 'f_web_res', 'f_final_img']:
                            if k in st.session_state: del st.session_state[k]
                            
                        if st.session_state.get('return_to_image_mode'):
                            st.session_state.entry_mode = 'image'
                            st.session_state['return_to_image_mode'] = False
                        else: st.session_state.entry_mode = 'manual'
                        st.rerun()

# ---------------------------------------------
# 画面②：商品マスター管理
# ---------------------------------------------
def master_manage_app():
    st.title("📦 商品マスター管理")
    CAT_MASTER = load_category_master()
    cat_list = list(CAT_MASTER.keys()) if CAT_MASTER else ["(カテゴリーなし)"]
    sh = connect_to_spreadsheet()
    prod_sheet = sh.worksheet("商品マスター")
    prod_data = prod_sheet.get_all_records() if len(prod_sheet.get_all_values()) > 1 else []
                
    tab1, tab2, tab3 = st.tabs(["✨ 新規登録", "✏️ マスターの変更", "📁 CSV一括登録"])
    
    with tab1:
        st.write("### 新しい商品をマスターに登録します")
        selected_cat = st.selectbox("登録するカテゴリーを選択", cat_list, key="m_cat")
        labels = CAT_MASTER.get(selected_cat, ["規格1", "規格2", "規格3"])
        
        max_id = 0
        for row in prod_data:
            if str(row.get('商品ID', '')).startswith('P'):
                try: max_id = max(max_id, int(str(row['商品ID'])[1:]))
                except: pass
        next_id = f"P{max_id + 1:03d}"
        
        with st.container(border=True):
            st.info(f"自動割り当てID: **{next_id}**")
            new_name = st.text_input("商品名 *必須", key="m_name")
            new_maker = st.text_input("メーカー名", key="m_maker")
            col1, col2, col3 = st.columns(3)
            with col1: new_spec1 = st.text_input(labels[0], key="m_s1")
            with col2: new_spec2 = st.text_input(labels[1], key="m_s2")
            with col3: new_spec3 = st.text_input(labels[2], key="m_s3")
            new_origin = st.text_input("原産国", key="m_orig")
            
            st.markdown("---")
            st.write("### 🔍 商品画像をウェブで検索")
            search_q = st.text_input("検索キーワード", value=new_name, key="m_img_q")
            if st.button("ウェブから画像を検索", key="m_search_btn"):
                with st.spinner("検索中..."):
                    try:
                        st.session_state['m_web_res'] = DDGS().images(search_q, max_results=3)
                    except Exception:
                        st.error("検索エラー。")
            
            if 'm_web_res' in st.session_state and st.session_state['m_web_res']:
                cols = st.columns(3) # ★ここも必ず3つ一列の枠を作る
                for i, res in enumerate(st.session_state['m_web_res'][:3]):
                    with cols[i]:
                        st.image(res['image'], use_container_width=True)
                        if st.button("👆 選択", key=f"m_sel_{i}", use_container_width=True):
                            with st.spinner("画像を取得中..."):
                                try:
                                    img_resp = requests.get(res['image'], timeout=10)
                                    st.session_state['m_final_img'] = img_resp.content
                                    st.success("画像を取得しました！")
                                except Exception:
                                    st.error("画像の取得に失敗しました。")
            
            if 'm_final_img' in st.session_state:
                st.image(st.session_state['m_final_img'], caption="✓ 登録予定の画像", width=200)
                
            st.write("▼ または手動で画像をアップロード")
            new_img_file = st.file_uploader("手元の画像をアップロード", type=["jpg", "jpeg", "png"], key="m_up")
            
            if st.button("💾 この内容で新規登録", type="primary", use_container_width=True):
                if not new_name: st.error("商品名は必須です。")
                else:
                    with st.spinner("保存中..."):
                        img_path = ""
                        if new_img_file:
                            optimized = optimize_image_in_memory(new_img_file)
                            img_path = save_compressed_image(optimized, next_id)
                        elif 'm_final_img' in st.session_state:
                            pil_img = Image.open(io.BytesIO(st.session_state['m_final_img']))
                            optimized = optimize_image_in_memory(pil_img)
                            img_path = save_compressed_image(optimized, next_id)
                            
                        prod_sheet.append_row([next_id, selected_cat, new_name, new_maker, new_spec1, new_spec2, new_spec3, new_origin, img_path])
                        st.success(f"【{new_name}】を登録しました！")
                        if 'm_web_res' in st.session_state: del st.session_state['m_web_res']
                        if 'm_final_img' in st.session_state: del st.session_state['m_final_img']
                        st.rerun()

    with tab2:
        st.write("### 登録済みマスターの情報を編集します")
        if not prod_data: st.warning("登録されている商品がありません。")
        else:
            f_cat_list = ["すべて"] + list(set([str(r.get('カテゴリー', '')) for r in prod_data if r.get('カテゴリー')]))
            f_maker_list = ["すべて"] + list(set([str(r.get('メーカー名', '')) for r in prod_data if r.get('メーカー名')]))
            col_s1, col_s2, col_s3 = st.columns(3)
            with col_s1: search_cat = st.selectbox("カテゴリー検索", f_cat_list)
            with col_s2: search_maker = st.selectbox("メーカー検索", f_maker_list)
            with col_s3: search_word = st.text_input("商品名（キーワード）")
            
            filtered_data = [d for d in prod_data if (search_cat == "すべて" or str(d.get('カテゴリー')) == search_cat) and (search_maker == "すべて" or str(d.get('メーカー名')) == search_maker) and (search_word in str(d.get('商品名', '')))]
            prod_dict = {f"[{row['商品ID']}] {row.get('商品名', '')}": row for row in filtered_data}
            if not prod_dict: st.warning("条件に一致する商品がありません。")
            else:
                selected_prod_key = st.selectbox("編集する商品を選択してください", ["選択してください"] + list(prod_dict.keys()))
                if selected_prod_key != "選択してください":
                    target_prod = prod_dict[selected_prod_key]
                    prod_id, row_idx = target_prod['商品ID'], list(prod_dict.keys()).index(selected_prod_key) + 2
                    
                    try: cat_idx = cat_list.index(target_prod.get('カテゴリー'))
                    except ValueError: cat_idx = 0
                    edit_cat = st.selectbox("カテゴリーの変更", cat_list, index=cat_idx, key=f"e_cat_{prod_id}")
                    labels = CAT_MASTER.get(edit_cat, ["規格1", "規格2", "規格3"])
                    
                    with st.form("edit_product_form"):
                        edit_name = st.text_input("商品名", value=target_prod.get('商品名', ''))
                        edit_maker = st.text_input("メーカー名", value=target_prod.get('メーカー名', ''))
                        col_e1, col_e2, col_e3 = st.columns(3)
                        with col_e1: edit_spec1 = st.text_input(labels[0], value=target_prod.get('規格1', ''))
                        with col_e2: edit_spec2 = st.text_input(labels[1], value=target_prod.get('規格2', ''))
                        with col_e3: edit_spec3 = st.text_input(labels[2], value=target_prod.get('規格3', ''))
                        edit_origin = st.text_input("原産国", value=target_prod.get('原産国', ''))
                        
                        current_img = target_prod.get('商品画像URL', '')
                        if current_img.startswith("http"): st.image(current_img, width=200)
                        edit_img_file = st.file_uploader("新しい画像で上書き", type=["jpg", "jpeg", "png"])
                        
                        if st.form_submit_button("🔄 変更を保存する"):
                            with st.spinner("クラウドに変更を保存中..."):
                                final_img_path = current_img
                                if edit_img_file:
                                    final_img_path = save_compressed_image(optimize_image_in_memory(edit_img_file), prod_id)
                                try:
                                    prod_sheet.update(range_name=f'A{row_idx}:I{row_idx}', values=[[prod_id, edit_cat, edit_name, edit_maker, edit_spec1, edit_spec2, edit_spec3, edit_origin, final_img_path]])
                                    st.success("更新しました！")
                                    st.rerun()
                                except Exception as e: st.error(f"更新エラー: {e}")

    with tab3:
        st.write("### 📁 CSVファイルから商品を一括登録します")
        uploaded_csv = st.file_uploader("CSVファイルをアップロード", type=["csv"])
        if uploaded_csv:
            try:
                try: df = pd.read_csv(uploaded_csv, encoding='utf-8')
                except: uploaded_csv.seek(0); df = pd.read_csv(uploaded_csv, encoding='shift_jis')
                if not df.empty:
                    st.dataframe(df.head())
                    if st.button("🚀 一括登録を実行", type="primary"):
                        prod_sheet.append_rows(df.fillna("").values.tolist())
                        st.success(f"✅ {len(df)}件を登録しました！")
                        st.rerun()
            except Exception as e: st.error("エラーが発生しました")

# ---------------------------------------------
# 画面③：商談向け分析ダッシュボード
# ---------------------------------------------
def dashboard_app():
    st.title("📊 商談向け分析ダッシュボード")
    sh = connect_to_spreadsheet()
    try:
        all_values = sh.worksheet("店舗別価格表").get_all_values()
        price_df = pd.DataFrame(all_values[1:], columns=all_values[0]).loc[:, ~pd.DataFrame(all_values[1:], columns=all_values[0]).columns.duplicated()] if len(all_values) > 1 else pd.DataFrame()
    except: price_df = pd.DataFrame()
        
    PROD_MASTER = load_product_master()
    prod_df = pd.DataFrame(list(PROD_MASTER.values())).loc[:, ~pd.DataFrame(list(PROD_MASTER.values())).columns.duplicated()] if PROD_MASTER else pd.DataFrame()
    
    if price_df.empty: return st.warning("価格データがありません。")
        
    for col in ['現行売価(税抜)', '現行売価(税込)']:
        if col in price_df.columns: price_df[col] = pd.to_numeric(price_df[col], errors='coerce')
    price_df['比較用価格'] = price_df['現行売価(税抜)'].fillna(price_df.get('現行売価(税込)'))
    
    if not prod_df.empty and '商品ID' in price_df.columns:
        kikaku_cols = [c for c in prod_df.columns if str(c).startswith('規格')]
        merge_cols = [c for c in ['商品ID', 'カテゴリー', 'メーカー名', '商品画像URL'] + kikaku_cols if c in prod_df.columns]
        merged_df = pd.merge(price_df, prod_df[merge_cols], on='商品ID', how='left')
    else:
        merged_df = price_df
        merged_df['カテゴリー'], merged_df['メーカー名'] = "不明", "不明"
        
    tab1, tab2, tab3, tab4 = st.tabs(["🛍️ 価格一覧", "🥇 最安値", "📊 マトリクス", "⚖️ ガチンコ比較"])
    
    with tab1:
        st.write("### 🛍️ 商品・店舗 価格一覧（画像・スペック付）")
        categories = [c for c in merged_df['カテゴリー'].unique() if pd.notna(c) and str(c).strip() != ""]
        if categories:
            sel_cat = st.selectbox("カテゴリーを選択", ["すべて"] + categories, key="t1_cat")
            disp_df = merged_df.copy() if sel_cat == "すべて" else merged_df[merged_df['カテゴリー'] == sel_cat].copy()
            disp_df = disp_df.dropna(subset=['比較用価格']).sort_values(['商品名', '比較用価格'])
            
            if not disp_df.empty:
                cols_to_show, col_config = [], {}
                if '商品画像URL' in disp_df.columns:
                    cols_to_show.append('商品画像URL'); col_config['商品画像URL'] = st.column_config.ImageColumn("画像")
                cols_to_show.extend(['販売店名', 'メーカー名', '商品名'])
                cols_to_show.extend([c for c in disp_df.columns if str(c).startswith('規格')])
                for col, name in [('現行売価(税抜)', "税抜"), ('現行売価(税込)', "税込")]:
                    if col in disp_df.columns:
                        cols_to_show.append(col); col_config[col] = st.column_config.NumberColumn(name, format="%d円")
                if '更新日' in disp_df.columns: cols_to_show.append('更新日')
                st.dataframe(disp_df[cols_to_show], column_config=col_config, hide_index=True, use_container_width=True)
            else: st.info("データがありません。")

    with tab2:
        st.write("### 🥇 カテゴリー別 最安値ランキング")
        if categories:
            sel_cat2 = st.selectbox("ランキングのカテゴリー", categories, key="t2_cat")
            ranked_df = merged_df[merged_df['カテゴリー'] == sel_cat2].dropna(subset=['比較用価格']).sort_values('比較用価格')
            if not ranked_df.empty: st.dataframe(ranked_df[['販売店名', '商品名', 'メーカー名', '現行売価(税抜)', '現行売価(税込)', '更新日']], hide_index=True, use_container_width=True)

    with tab3:
        st.write("### 📊 エリア価格一覧表（マトリクス）")
        if not merged_df.empty:
            try: st.dataframe(merged_df.dropna(subset=['比較用価格']).pivot_table(index=['メーカー名', '商品名'], columns='販売店名', values='比較用価格', aggfunc='min').reset_index(), hide_index=True, use_container_width=True)
            except: pass

    with tab4:
        st.write("### ⚖️ 店舗間 ガチンコ比較（全アイテム一覧）")
        stores = [s for s in merged_df['販売店名'].unique() if pd.notna(s) and str(s).strip() != ""]
        if len(stores) >= 2:
            sel_cat_t4 = st.selectbox("比較カテゴリー", ["すべて"] + [c for c in prod_df['カテゴリー'].unique() if pd.notna(c) and str(c).strip() != ""], key="t4_cat")
            col_a, col_b = st.columns(2)
            with col_a: store_A = st.selectbox("比較元 (店舗A)", stores, key="comp_A")
            with col_b: store_B = st.selectbox("比較先 (店舗B)", stores, key="comp_B")
            
            if store_A and store_B and store_A != store_B:
                comp_df = prod_df.copy()
                df_A = merged_df[merged_df['販売店名'] == store_A][['商品ID', '比較用価格']].rename(columns={'比較用価格': f'{store_A}の価格'}).drop_duplicates('商品ID', keep='last')
                df_B = merged_df[merged_df['販売店名'] == store_B][['商品ID', '比較用価格']].rename(columns={'比較用価格': f'{store_B}の価格'}).drop_duplicates('商品ID', keep='last')
                
                comp_df = pd.merge(pd.merge(comp_df, df_A, on='商品ID', how='left'), df_B, on='商品ID', how='left')
                if sel_cat_t4 != "すべて": comp_df = comp_df[comp_df['カテゴリー'] == sel_cat_t4]
                    
                if not comp_df.empty:
                    comp_df['価格差 (A - B)'] = comp_df[f'{store_A}の価格'] - comp_df[f'{store_B}の価格']
                    comp_df = comp_df.sort_values([c for c in ['メーカー名', '商品名'] if c in comp_df.columns])
                    
                    cols_t4, conf_t4 = [], {}
                    if '商品画像URL' in comp_df.columns: cols_t4.append('商品画像URL'); conf_t4['商品画像URL'] = st.column_config.ImageColumn("画像")
                    if sel_cat_t4 == "すべて" and 'カテゴリー' in comp_df.columns: cols_t4.append('カテゴリー')
                    cols_t4.extend([c for c in ['メーカー名', '商品名'] if c in comp_df.columns])
                    cols_t4.extend([c for c in comp_df.columns if str(c).startswith('規格')])
                    
                    for col, name in [(f'{store_A}の価格', store_A), (f'{store_B}の価格', store_B), ('価格差 (A - B)', "差額(A-B)")]:
                        cols_t4.append(col); conf_t4[col] = st.column_config.NumberColumn(name, format="%d円")
                        
                    st.dataframe(comp_df[cols_t4], column_config=conf_t4, hide_index=True, use_container_width=True)

# ==========================================
# 5. メインコントローラー
# ==========================================
if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False

if st.session_state['logged_in']:
    st.sidebar.title("メニュー")
    st.sidebar.write(f"👤 **{st.session_state.get('user_name', 'ゲスト')}** さん")
    
    menu_selection = st.sidebar.radio("機能を選択", ["① 売価チェック", "② 商品マスター管理", "③ 分析ダッシュボード"])
    st.sidebar.markdown("---")
    if st.sidebar.button("ログアウト"):
        st.session_state['logged_in'], st.session_state['auth_stage'] = False, 1
        st.rerun()

    if menu_selection == "① 売価チェック": price_check_app()
    elif menu_selection == "② 商品マスター管理": master_manage_app()
    elif menu_selection == "③ 分析ダッシュボード": dashboard_app()
else:
    login_screen()