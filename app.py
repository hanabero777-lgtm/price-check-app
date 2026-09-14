import streamlit as st
import google.generativeai as genai
import pandas as pd
from PIL import Image
Image.MAX_IMAGE_PIXELS = None 

import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os
import difflib
import io
import requests
import base64

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
        if not store_data:
            return ["選択してください"]
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
        cat_dict[row["カテゴリー名"]] = [
            row.get("規格1ラベル", "規格1"),
            row.get("規格2ラベル", "規格2"),
            row.get("規格3ラベル", "規格3")
        ]
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
        if len(all_values) <= 1:
            return {}
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
    if not ai_name or not prod_master:
        return []
    scores = []
    for key, data in prod_master.items():
        master_name = data.get('商品名', '')
        score = difflib.SequenceMatcher(None, ai_name, master_name).ratio()
        scores.append((score, key))
    scores.sort(reverse=True, key=lambda x: x[0])
    return [item[1] for item in scores[:top_n] if item[0] > 0.15]

def save_or_update_price(store_name, prod_id, prod_name, price_notax, price_tax, user_name):
    sh = connect_to_spreadsheet()
    price_sheet = sh.worksheet("店舗別価格表")
    today = datetime.now().strftime("%Y/%m/%d")

    headers = price_sheet.row_values(1)
    required_headers = [
        "販売店名", "商品ID", "商品名", "現行売価(税抜)", "現行売価(税込)", "更新日", "担当者",
        "過去売価1(税抜)", "過去売価1(税込)", "過去日1", "過去売価2(税抜)", "過去売価2(税込)", "過去日2"
    ]
    if len(headers) < len(required_headers):
        for i, h in enumerate(required_headers):
            if i >= len(headers):
                price_sheet.update_cell(1, i+1, h)

    all_values = price_sheet.get_all_values()
    found_idx = -1
    
    for i, row in enumerate(all_values):
        if i == 0: continue
        if len(row) >= 2 and row[0] == store_name and row[1] == prod_id:
            found_idx = i + 1
            break

    in_notax = str(price_notax)
    in_tax = str(price_tax)

    if found_idx != -1:
        row = all_values[found_idx - 1]
        padded_row = row + [""] * (13 - len(row))
        
        curr_notax = str(padded_row[3])
        curr_tax = str(padded_row[4])
        curr_date = padded_row[5]
        hist1_notax = padded_row[7]
        hist1_tax = padded_row[8]
        hist1_date = padded_row[9]

        if curr_notax == in_notax and curr_tax == in_tax:
            price_sheet.update(f'F{found_idx}:G{found_idx}', [[today, user_name]])
        else:
            new_values = [
                in_notax, in_tax, today, user_name,            
                curr_notax, curr_tax, curr_date,               
                hist1_notax, hist1_tax, hist1_date             
            ]
            price_sheet.update(f'D{found_idx}:M{found_idx}', [new_values])
    else:
        new_row = [
            store_name, prod_id, prod_name, in_notax, in_tax, today, user_name,
            "", "", "", "", "", ""
        ]
        price_sheet.append_row(new_row)

# ==========================================
# 4. 画面ごとの関数
# ==========================================
def authenticate_user(username, password):
    try:
        sh = connect_to_spreadsheet()
        user_sheet = sh.worksheet("ユーザー管理")
        users = user_sheet.get_all_records()
        for user in users:
            if str(user.get("ユーザーID", "")) == username and str(user.get("パスワード", "")) == password:
                pin = str(user.get("PINコード", "")) 
                return True, user.get("氏名", "不明"), pin
        return False, "", ""
    except Exception as e:
        st.error(f"認証エラーが発生しました: {e}")
        return False, "", ""

def login_screen():
    st.title("🔐 システムログイン")
    
    if 'auth_stage' not in st.session_state:
        st.session_state['auth_stage'] = 1
        
    if st.session_state['auth_stage'] == 1:
        st.write("支給されたIDとパスワードを入力してください。")
        username = st.text_input("ユーザーID")
        password = st.text_input("パスワード", type="password")
        
        if st.button("次へ", type="primary", use_container_width=True):
            with st.spinner("認証中..."):
                is_auth, name, expected_pin = authenticate_user(username, password)
                if is_auth:
                    st.session_state['temp_name'] = name
                    st.session_state['expected_pin'] = expected_pin
                    st.session_state['auth_stage'] = 2
                    st.rerun()
                else:
                    st.error("ユーザーIDかパスワードが間違っています。")
                    
    elif st.session_state['auth_stage'] == 2:
        st.write(f"👤 **{st.session_state['temp_name']}** さん")
        st.write("二段階認証：4桁のPINコードを入力してください。")
        entered_pin = st.text_input("PINコード", type="password", placeholder="****")
        
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
                    st.session_state['logged_in'] = True
                    st.session_state['user_name'] = st.session_state['temp_name']
                    st.session_state['auth_stage'] = 1 
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
            
        selected_store = st.selectbox("販売店を選択", STORE_MASTER, index=idx)
        st.session_state.selected_store_val = selected_store
        
        with st.expander("➕ 新しい販売店を追加する場合はこちら"):
            new_store_name = st.text_input("販売店名", placeholder="例: Eマート 加茂店")
            new_area_name = st.text_input("エリア", placeholder="例: 加茂市")
            if st.button("マスターに登録"):
                if new_store_name:
                    with st.spinner("登録中..."):
                        if add_new_store(new_store_name, new_area_name):
                            st.success(f"「{new_store_name}」を登録しました！")
                            st.rerun()

        st.markdown("---")
        st.write("### 2. 商品の登録方法を選択")
        col1, col2 = st.columns(2)
        with col1:
            if st.button("📸 画像で一括判別\n\n(AIが自動読み取り)", type="primary", use_container_width=True):
                if st.session_state.selected_store_val == "選択してください":
                    st.error("先に販売店を選択してください。")
                else:
                    st.session_state.entry_mode = 'image'
                    st.rerun()
        with col2:
            if st.button("🔍 アイテム名で検索\n\n(手動で探して登録)", type="primary", use_container_width=True):
                if st.session_state.selected_store_val == "選択してください":
                    st.error("先に販売店を選択してください。")
                else:
                    st.session_state.entry_mode = 'manual'
                    st.rerun()

    elif st.session_state.entry_mode == 'image':
        if st.button("⬅️ 店舗・入力方法の選択に戻る"):
            st.session_state.entry_mode = None
            if 'scanned_data' in st.session_state: del st.session_state['scanned_data']
            st.rerun()
            
        st.info(f"🏢 **対象店舗:** {st.session_state.selected_store_val}")
        st.write("### 📸 画像から商品を読み取る")
        
        uploaded_file = st.file_uploader("棚の画像を撮影 / 選択", type=["jpg", "jpeg", "png"])
        if uploaded_file is not None:
            image = optimize_image_in_memory(uploaded_file)
            st.image(image, caption="アップロード画像 (最適化済)", use_container_width=True)

            if st.button("🤖 AIで商品を解析する"):
                with st.spinner("AIが解析中です..."):
                    try:
                        prompt = """
                        この画像に写っている全ての商品と、その売価を読み取ってください。
                        以下のJSONフォーマットで、テキストのみを出力してください。
                        [
                          {"カテゴリー": "トイレットペーパー", "商品名": "商品A", "価格": 298, "税区分": "税別"}
                        ]
                        """
                        response = model.generate_content([prompt, image])
                        result_text = response.text.strip().replace("```json", "").replace("```", "")
                        st.session_state['scanned_data'] = json.loads(result_text)
                        st.success("読み取り完了！下にスクロールしてマスターと紐付けてください。")
                    except Exception as e:
                        st.error(f"エラーが発生しました: {e}")

        if 'scanned_data' in st.session_state:
            st.markdown("---")
            st.write("### 3. 商品の紐付けと結果の確認")
            PROD_MASTER = load_product_master()
            all_options = ["選択してください (マスター該当なし)"] + list(PROD_MASTER.keys())
            
            for i, item in enumerate(st.session_state['scanned_data']):
                ai_name = item.get('商品名', '不明')
                ai_price = int(item.get('価格', 0))
                ai_tax = item.get('税区分', '税別')
                
                state_key = f"selected_master_{i}"
                candidates = get_top_candidates(ai_name, PROD_MASTER)
                if state_key not in st.session_state:
                    st.session_state[state_key] = candidates[0] if candidates else all_options[0]

                with st.container(border=True):
                    st.markdown(f"### 🔍 AI読取結果: `{ai_name}`")
                    if candidates:
                        cand_cols = st.columns(min(len(candidates), 3))
                        for c_idx, cand_key in enumerate(candidates[:3]):
                            with cand_cols[c_idx]:
                                cand_img = PROD_MASTER[cand_key].get('商品画像URL', '')
                                if cand_img and (cand_img.startswith("http") or os.path.exists(cand_img)):
                                    st.image(cand_img, use_container_width=True)
                                else:
                                    st.info("画像なし")
                                if st.button("👆 これを選択", key=f"btn_{i}_{c_idx}"):
                                    st.session_state[state_key] = cand_key
                                    st.rerun()
                    
                    st.markdown("---")
                    col_img, col_form = st.columns([1, 1])
                    with col_img:
                        selected_master = st.session_state.get(state_key, all_options[0])
                        if selected_master != all_options[0]:
                            img_path = PROD_MASTER[selected_master].get('商品画像URL', '')
                            if img_path and (img_path.startswith("http") or os.path.exists(img_path)):
                                st.image(img_path, use_container_width=True, caption=PROD_MASTER[selected_master].get('商品名'))
                            else:
                                st.info("📷 画像未登録")
                        else:
                            st.warning("⚠️ マスター未選択")
                    with col_form:
                        if st.session_state[state_key] not in all_options: st.session_state[state_key] = all_options[0]
                        st.selectbox("🔗 紐付けマスター", options=all_options, key=state_key)
                        st.number_input("💰 売価", value=ai_price, step=1, key=f"price_{i}")
                        st.selectbox("🧾 税区分", ["税別", "税込"], index=0 if ai_tax == '税別' else 1, key=f"tax_{i}")
                            
            if st.button("💾 全て確認してスプレッドシートに登録", type="primary", use_container_width=True):
                with st.spinner("保存中..."):
                    try:
                        for i, item in enumerate(st.session_state['scanned_data']):
                            state_key = f"selected_master_{i}"
                            final_price = st.session_state[f"price_{i}"]
                            final_tax = st.session_state[f"tax_{i}"]
                            final_master = st.session_state[state_key]
                            
                            prod_id, prod_name = "", item.get('商品名', '不明')
                            if final_master != all_options[0]:
                                prod_id = PROD_MASTER[final_master]['商品ID']
                                prod_name = PROD_MASTER[final_master].get('商品名', '')
                                
                            price_notax = final_price if final_tax == '税別' else ""
                            price_tax = final_price if final_tax == '税込' else ""
                            
                            save_or_update_price(
                                st.session_state.selected_store_val, 
                                prod_id, 
                                prod_name, 
                                price_notax, 
                                price_tax, 
                                st.session_state.get('user_name', '不明')
                            )

                        st.success("✅ スプレッドシートへの登録・更新が完了しました！")
                        del st.session_state['scanned_data']
                        st.rerun()
                    except Exception as e:
                        st.error(f"保存に失敗しました: {e}")

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
        
        CAT_MASTER = load_category_master()
        PROD_MASTER = load_product_master()
        prod_data = list(PROD_MASTER.values())
        
        if not prod_data:
            st.warning("商品マスターが登録されていません。上のボタンから新しく登録してください。")
        else:
            with st.expander("▼ 検索フィルターを開く", expanded=True):
                cat_list = ["すべて"] + list(set([str(r.get('カテゴリー', '')) for r in prod_data if r.get('カテゴリー')]))
                maker_list = ["すべて"] + list(set([str(r.get('メーカー名', '')) for r in prod_data if r.get('メーカー名')]))
                col_s1, col_s2, col_s3 = st.columns(3)
                with col_s1: search_cat = st.selectbox("カテゴリー", cat_list)
                with col_s2: search_maker = st.selectbox("メーカー", maker_list)
                with col_s3: search_word = st.text_input("商品名（キーワード）")
                
            filtered_data = []
            for d in prod_data:
                if (search_cat == "すべて" or str(d.get('カテゴリー')) == search_cat) and \
                   (search_maker == "すべて" or str(d.get('メーカー名')) == search_maker) and \
                   (search_word in str(d.get('商品名', ''))):
                    filtered_data.append(d)
                    
            prod_dict = {f"[{row['商品ID']}] {row.get('商品名', '')}": row for row in filtered_data}
            
            st.markdown("---")
            if not prod_dict:
                st.warning("条件に一致する商品がありません。")
            else:
                selected_prod_key = st.selectbox("登録する商品を選択", ["選択してください"] + list(prod_dict.keys()))
                
                if selected_prod_key != "選択してください":
                    target_prod = prod_dict[selected_prod_key]
                    
                    with st.container(border=True):
                        col_img, col_form = st.columns([1, 1])
                        with col_img:
                            img_path = target_prod.get('商品画像URL', '')
                            if img_path and (img_path.startswith("http") or os.path.exists(img_path)):
                                st.image(img_path, use_container_width=True)
                            else:
                                st.info("📷 画像未登録")
                                
                        with col_form:
                            st.write(f"### {target_prod.get('商品名')}")
                            st.write(f"**カテゴリー:** {target_prod.get('カテゴリー')}")
                            st.write(f"**メーカー:** {target_prod.get('メーカー名')}")
                            
                            spec1 = target_prod.get('規格1', target_prod.get('規格１', ''))
                            spec2 = target_prod.get('規格2', target_prod.get('規格２', ''))
                            spec3 = target_prod.get('規格3', target_prod.get('規格３', ''))
                            
                            cat_name = target_prod.get('カテゴリー', '')
                            labels = CAT_MASTER.get(cat_name, ["規格1", "規格2", "規格3"])
                            
                            spec_text = []
                            if spec1: spec_text.append(f"{labels[0]}: {spec1}")
                            if spec2: spec_text.append(f"{labels[1]}: {spec2}")
                            if spec3: spec_text.append(f"{labels[2]}: {spec3}")
                            
                            st.write(f"**規格:** {' / '.join(spec_text)}" if spec_text else "**規格:** 未登録")
                            
                            st.markdown("---")
                            with st.form(key="manual_price_form"):
                                man_price = st.number_input("💰 売価を入力", value=0, step=1)
                                man_tax = st.selectbox("🧾 税区分", ["税別", "税込"])
                                if st.form_submit_button("💾 スプレッドシートに登録", type="primary", use_container_width=True):
                                    if man_price <= 0:
                                        st.error("売価を正しく入力してください。")
                                    else:
                                        with st.spinner("保存中..."):
                                            try:
                                                price_notax = man_price if man_tax == '税別' else ""
                                                price_tax = man_price if man_tax == '税込' else ""
                                                
                                                save_or_update_price(
                                                    st.session_state.selected_store_val,
                                                    target_prod['商品ID'], 
                                                    target_prod.get('商品名', ''),
                                                    price_notax, 
                                                    price_tax,
                                                    st.session_state.get('user_name', '不明')
                                                )
                                                st.success("✅ 登録しました！")
                                            except Exception as e:
                                                st.error(f"保存失敗: {e}")

    elif st.session_state.entry_mode == 'new_product_from_field':
        if st.button("⬅️ 検索画面に戻る"):
            st.session_state.entry_mode = 'manual'
            st.rerun()
            
        st.write("### ✨ 新しい商品をマスターに登録")
        
        CAT_MASTER = load_category_master()
        cat_list = list(CAT_MASTER.keys()) if CAT_MASTER else ["(カテゴリーなし)"]
        
        with st.expander("➕ 新しいカテゴリーを作る場合はこちら"):
            n_cat = st.text_input("カテゴリー名", key="f_cat")
            n_l1 = st.text_input("規格1の項目名", placeholder="例: 組数", key="f_l1")
            n_l2 = st.text_input("規格2の項目名", placeholder="例: パック入数", key="f_l2")
            n_l3 = st.text_input("規格3の項目名", placeholder="例: 紙サイズ", key="f_l3")
            if st.button("カテゴリーマスターに追加", key="f_btn"):
                if n_cat:
                    add_new_category(n_cat, n_l1 or "規格1", n_l2 or "規格2", n_l3 or "規格3")
                    st.success(f"「{n_cat}」を追加しました！")
                    st.rerun()

        selected_cat = st.selectbox("カテゴリーを選択", cat_list, key="f_cat_sel")
        labels = CAT_MASTER.get(selected_cat, ["規格1", "規格2", "規格3"])
        
        try:
            sh = connect_to_spreadsheet()
            prod_sheet = sh.worksheet("商品マスター")
            max_id = 0
            for row in prod_sheet.get_all_records():
                if row.get('商品ID', '').startswith('P'):
                    try:
                        num = int(str(row['商品ID'])[1:])
                        if num > max_id: max_id = num
                    except: pass
            next_id = f"P{max_id + 1:03d}"
            
            with st.container(border=True):
                st.info(f"自動割り当てID: **{next_id}**")
                with st.form("new_product_field_form"):
                    st.write(f"**選択中のカテゴリー:** {selected_cat}")
                    new_name = st.text_input("商品名 *必須")
                    new_maker = st.text_input("メーカー名")
                    col1, col2, col3 = st.columns(3)
                    with col1: new_spec1 = st.text_input(labels[0])
                    with col2: new_spec2 = st.text_input(labels[1])
                    with col3: new_spec3 = st.text_input(labels[2])
                    new_origin = st.text_input("原産国")
                    new_img_file = st.file_uploader("商品画像をアップロード", type=["jpg", "jpeg", "png"])
                    
                    if st.form_submit_button("💾 マスターに登録して検索画面に戻る", type="primary", use_container_width=True):
                        if not new_name:
                            st.error("商品名は必須です。")
                        else:
                            with st.spinner("クラウド（ImgBB）に画像を保存中..."):
                                img_path = ""
                                if new_img_file:
                                    optimized_img = optimize_image_in_memory(new_img_file)
                                    img_path = save_compressed_image(optimized_img, next_id)
                                prod_sheet.append_row([
                                    next_id, selected_cat, new_name, new_maker, 
                                    new_spec1, new_spec2, new_spec3, new_origin, img_path
                                ])
                                st.success("登録完了！")
                                st.session_state.entry_mode = 'manual'
                                st.rerun()
        except Exception as e:
            st.error(f"通信エラー: {e}")

# ---------------------------------------------
# 画面②：商品マスター管理
# ---------------------------------------------
def master_manage_app():
    st.title("📦 商品マスター管理")
    
    CAT_MASTER = load_category_master()
    cat_list = list(CAT_MASTER.keys()) if CAT_MASTER else ["(カテゴリーなし)"]
    
    sh = connect_to_spreadsheet()
    prod_sheet = sh.worksheet("商品マスター")
    
    all_values = prod_sheet.get_all_values()
    prod_data = []
    if len(all_values) > 1:
        headers = all_values[0]
        for i, row in enumerate(all_values[1:], start=2):
            if row and len(row) > 0 and row[0]:
                padded_row = row + [""] * (len(headers) - len(row))
                rd = {headers[j]: padded_row[j] for j in range(len(headers))}
                rd['_row_idx'] = i
                prod_data.append(rd)
                
    tab1, tab2, tab3 = st.tabs(["✨ 新規登録", "✏️ マスターの変更", "📁 CSV一括登録"])
    
    with tab1:
        st.write("### 新しい商品をマスターに登録します")
        with st.expander("➕ 新しいカテゴリーをマスターに追加する場合はこちら"):
            n_cat = st.text_input("カテゴリー名")
            n_l1 = st.text_input("規格1の項目名", placeholder="例: 組数")
            n_l2 = st.text_input("規格2の項目名", placeholder="例: パック入数")
            n_l3 = st.text_input("規格3の項目名", placeholder="例: 紙サイズ")
            if st.button("カテゴリーマスターに追加"):
                if n_cat:
                    add_new_category(n_cat, n_l1 or "規格1", n_l2 or "規格2", n_l3 or "規格3")
                    st.success(f"「{n_cat}」を追加しました！")
                    st.rerun()
                    
        selected_cat = st.selectbox("登録するカテゴリーを選択してください", cat_list, key="tab1_cat")
        labels = CAT_MASTER.get(selected_cat, ["規格1", "規格2", "規格3"])
        
        max_id = 0
        for row in prod_data:
            if row.get('商品ID', '').startswith('P'):
                try:
                    num = int(str(row['商品ID'])[1:])
                    if num > max_id: max_id = num
                except: pass
        next_id = f"P{max_id + 1:03d}"
        st.info(f"次に割り振られる商品ID: **{next_id}**")
        
        with st.form("new_product_form"):
            st.write(f"**選択中のカテゴリー:** {selected_cat}")
            new_name = st.text_input("商品名 *必須")
            new_maker = st.text_input("メーカー名")
            col1, col2, col3 = st.columns(3)
            with col1: new_spec1 = st.text_input(labels[0])
            with col2: new_spec2 = st.text_input(labels[1])
            with col3: new_spec3 = st.text_input(labels[2])
            new_origin = st.text_input("原産国")
            new_img_file = st.file_uploader("商品画像をアップロード", type=["jpg", "jpeg", "png"])
            
            if st.form_submit_button("💾 この内容で新規登録"):
                if not new_name:
                    st.error("商品名は必須です。")
                else:
                    with st.spinner("クラウド（ImgBB）に画像を保存中..."):
                        img_path = ""
                        if new_img_file:
                            optimized_img = optimize_image_in_memory(new_img_file)
                            img_path = save_compressed_image(optimized_img, next_id)
                        prod_sheet.append_row([
                            next_id, selected_cat, new_name, new_maker, 
                            new_spec1, new_spec2, new_spec3, new_origin, img_path
                        ])
                        st.success(f"【{new_name}】をマスターに登録しました！")
                        st.rerun()

    with tab2:
        st.write("### 登録済みマスターの情報を編集します")
        if not prod_data:
            st.warning("登録されている商品がありません。")
        else:
            st.write("▼ 検索条件で絞り込む")
            f_cat_list = ["すべて"] + list(set([str(r.get('カテゴリー', '')) for r in prod_data if r.get('カテゴリー')]))
            f_maker_list = ["すべて"] + list(set([str(r.get('メーカー名', '')) for r in prod_data if r.get('メーカー名')]))
            col_s1, col_s2, col_s3 = st.columns(3)
            with col_s1: search_cat = st.selectbox("カテゴリー検索", f_cat_list)
            with col_s2: search_maker = st.selectbox("メーカー検索", f_maker_list)
            with col_s3: search_word = st.text_input("商品名（キーワード）")
            
            filtered_data = []
            for d in prod_data:
                if (search_cat == "すべて" or str(d.get('カテゴリー')) == search_cat) and \
                   (search_maker == "すべて" or str(d.get('メーカー名')) == search_maker) and \
                   (search_word in str(d.get('商品名', ''))):
                    filtered_data.append(d)
                    
            prod_dict = {f"[{row['商品ID']}] {row.get('商品名', '')}": row for row in filtered_data}
            if not prod_dict:
                st.warning("条件に一致する商品がありません。")
            else:
                selected_prod_key = st.selectbox("編集する商品を選択してください", ["選択してください"] + list(prod_dict.keys()))
                if selected_prod_key != "選択してください":
                    target_prod = prod_dict[selected_prod_key]
                    prod_id = target_prod['商品ID']
                    row_idx = target_prod['_row_idx']
                    st.markdown("---")
                    
                    try: cat_idx = cat_list.index(target_prod.get('カテゴリー'))
                    except ValueError: cat_idx = 0
                    edit_cat = st.selectbox("カテゴリーの変更", cat_list, index=cat_idx, key=f"edit_cat_{prod_id}")
                    labels = CAT_MASTER.get(edit_cat, ["規格1", "規格2", "規格3"])
                    
                    spec1 = target_prod.get('規格1', target_prod.get('規格１', ''))
                    spec2 = target_prod.get('規格2', target_prod.get('規格２', ''))
                    spec3 = target_prod.get('規格3', target_prod.get('規格３', ''))
                    
                    with st.form("edit_product_form"):
                        st.write(f"**商品ID: {prod_id} の編集**")
                        edit_name = st.text_input("商品名", value=target_prod.get('商品名', ''))
                        edit_maker = st.text_input("メーカー名", value=target_prod.get('メーカー名', ''))
                        col_e1, col_e2, col_e3 = st.columns(3)
                        with col_e1: edit_spec1 = st.text_input(labels[0], value=spec1)
                        with col_e2: edit_spec2 = st.text_input(labels[1], value=spec2)
                        with col_e3: edit_spec3 = st.text_input(labels[2], value=spec3)
                        edit_origin = st.text_input("原産国", value=target_prod.get('原産国', ''))
                        
                        current_img_path = target_prod.get('商品画像URL', '')
                        st.write("▼ 画像の更新")
                        if current_img_path and (current_img_path.startswith("http") or os.path.exists(current_img_path)):
                            st.image(current_img_path, width=200, caption="現在登録されている画像")
                        else:
                            st.info("※現在登録されている画像はありません")
                            
                        edit_img_file = st.file_uploader("新しい画像で上書き", type=["jpg", "jpeg", "png"])
                        
                        if st.form_submit_button("🔄 変更を保存する"):
                            with st.spinner("クラウドに変更を保存中..."):
                                final_img_path = current_img_path
                                if edit_img_file:
                                    optimized_img = optimize_image_in_memory(edit_img_file)
                                    final_img_path = save_compressed_image(optimized_img, prod_id)
                                    
                                try:
                                    prod_sheet.update(
                                        range_name=f'A{row_idx}:I{row_idx}', 
                                        values=[[prod_id, edit_cat, edit_name, edit_maker, edit_spec1, edit_spec2, edit_spec3, edit_origin, final_img_path]]
                                    )
                                except Exception as e:
                                    st.error(f"更新エラー: {e}")
                                    
                                st.success("商品の情報を更新しました！")
                                st.rerun()

    with tab3:
        st.write("### 📁 CSVファイルから商品を一括登録します")
        st.info("エクセル等で作成した「商品マスター」のCSVファイルをアップロードしてください。")
        uploaded_csv = st.file_uploader("CSVファイルをアップロード", type=["csv"])
        
        if uploaded_csv is not None:
            try:
                try:
                    df = pd.read_csv(uploaded_csv, encoding='utf-8')
                except Exception:
                    uploaded_csv.seek(0)
                    df = pd.read_csv(uploaded_csv, encoding='shift_jis')
                    
                if not df.empty:
                    st.write("▼ 読み込みプレビュー (最初の5件)")
                    st.dataframe(df.head())
                    
                    if st.button("🚀 このデータで一括登録を実行", type="primary"):
                        with st.spinner("スプレッドシートに登録中..."):
                            try:
                                df = df.fillna("")
                                values = df.values.tolist()
                                prod_sheet.append_rows(values)
                                st.success(f"✅ {len(values)}件の商品を一括登録しました！")
                                st.rerun()
                            except Exception as e:
                                st.error(f"登録中にエラーが発生しました: {e}")
            except Exception as e:
                st.error(f"ファイルの読み込みに失敗しました: {e}")

# ---------------------------------------------
# 画面③：商談向け分析ダッシュボード (★修正・大幅強化版)
# ---------------------------------------------
def dashboard_app():
    st.title("📊 商談向け分析ダッシュボード")
    
    sh = connect_to_spreadsheet()
    try:
        price_sheet = sh.worksheet("店舗別価格表")
        # エラーを防ぐため、安全なデータ取得方法に変更
        all_values = price_sheet.get_all_values()
        if len(all_values) > 1:
            headers = all_values[0]
            price_df = pd.DataFrame(all_values[1:], columns=headers)
        else:
            price_df = pd.DataFrame()
    except Exception as e:
        st.error(f"データ取得エラー: {e}")
        price_df = pd.DataFrame()
        
    PROD_MASTER = load_product_master()
    if not PROD_MASTER:
        prod_df = pd.DataFrame()
    else:
        prod_data = list(PROD_MASTER.values())
        prod_df = pd.DataFrame(prod_data)
    
    if price_df.empty:
        st.warning("価格データがまだ登録されていないか、正しく読み込めませんでした。")
        return
        
    # 【重要修正】税抜・税込の文字を数字に変換。空欄は無視する。
    for col in ['現行売価(税抜)', '現行売価(税込)']:
        if col in price_df.columns:
            price_df[col] = pd.to_numeric(price_df[col], errors='coerce')
            
    # 【重要修正】「比較用価格」を自動生成（税抜を優先し、なければ税込を使う）
    price_df['比較用価格'] = price_df['現行売価(税抜)'].fillna(price_df['現行売価(税込)'])
    
    if not prod_df.empty and '商品ID' in price_df.columns:
        merged_df = pd.merge(price_df, prod_df[['商品ID', 'カテゴリー', '商品画像URL', 'メーカー名']], on='商品ID', how='left')
    else:
        merged_df = price_df
        merged_df['カテゴリー'] = "不明"
        merged_df['メーカー名'] = "不明"
        
    # 商談に特化した4つのタブ構成
    tab1, tab2, tab3, tab4 = st.tabs(["🛍️ 商品別の店舗比較", "🥇 カテゴリー別 最安値", "📊 エリア価格一覧(ﾏﾄﾘｸｽ)", "⚖️ 店舗間 ガチンコ比較"])
    
    with tab1:
        st.write("### 🛍️ 商品別の店舗比較（バイヤー提案用）")
        st.write("特定の商品が、各店舗でいくらで売られているかを棒グラフで比較します。")
        
        categories = [c for c in merged_df['カテゴリー'].unique() if pd.notna(c) and str(c).strip() != ""]
        if categories:
            sel_cat = st.selectbox("カテゴリーを選択", categories, key="t1_cat")
            cat_df = merged_df[merged_df['カテゴリー'] == sel_cat]
            products = [p for p in cat_df['商品名'].unique() if pd.notna(p) and str(p).strip() != ""]
            
            if products:
                sel_prod = st.selectbox("比較する商品を選択", products, key="t1_prod")
                prod_comp_df = cat_df[cat_df['商品名'] == sel_prod].dropna(subset=['比較用価格'])
                
                if not prod_comp_df.empty:
                    prod_comp_df = prod_comp_df.sort_values('比較用価格')
                    # 棒グラフを表示
                    st.bar_chart(prod_comp_df.set_index('販売店名')['比較用価格'])
                    
                    st.dataframe(
                        prod_comp_df[['販売店名', '現行売価(税抜)', '現行売価(税込)', '更新日', '担当者']],
                        hide_index=True, use_container_width=True
                    )
                else:
                    st.info("この商品の価格データはありません。")
        else:
            st.info("商品データがありません。")

    with tab2:
        st.write("### 🥇 カテゴリー別 最安値ランキング")
        if categories:
            sel_cat2 = st.selectbox("カテゴリーを選択してランキングを表示", categories, key="t2_cat")
            cat_df2 = merged_df[merged_df['カテゴリー'] == sel_cat2].copy()
            
            ranked_df = cat_df2.dropna(subset=['比較用価格']).sort_values(by='比較用価格', ascending=True)
            if not ranked_df.empty:
                st.dataframe(
                    ranked_df[['販売店名', '商品名', 'メーカー名', '現行売価(税抜)', '現行売価(税込)', '更新日']],
                    hide_index=True, use_container_width=True
                )
            else:
                st.info("ランキングデータがありません。")

    with tab3:
        st.write("### 📊 エリア価格一覧表（マトリクス）")
        st.write("店舗（横）× 商品（縦）で、エリア全体の価格相場を一目で把握できます。")
        
        if not merged_df.empty:
            try:
                pivot_df = merged_df.dropna(subset=['比較用価格']).pivot_table(
                    index=['メーカー名', '商品名'],
                    columns='販売店名',
                    values='比較用価格',
                    aggfunc='min'
                ).reset_index()
                st.dataframe(pivot_df, hide_index=True, use_container_width=True)
            except Exception:
                st.info("マトリクスを生成するためのデータが不足しています。")

    with tab4:
        st.write("### ⚖️ 店舗間 ガチンコ比較")
        stores = [s for s in merged_df['販売店名'].unique() if pd.notna(s) and str(s).strip() != ""]
        if len(stores) >= 2:
            col_a, col_b = st.columns(2)
            with col_a: store_A = st.selectbox("比較元 (販売店A)", stores, key="comp_A")
            with col_b: store_B = st.selectbox("比較先 (販売店B)", stores, key="comp_B")
            
            if store_A and store_B and store_A != store_B:
                df_A = merged_df[merged_df['販売店名'] == store_A][['商品ID', '商品名', '比較用価格']].rename(columns={'比較用価格': f'{store_A}価格'})
                df_B = merged_df[merged_df['販売店名'] == store_B][['商品ID', '比較用価格']].rename(columns={'比較用価格': f'{store_B}価格'})
                
                comp_df = pd.merge(df_A, df_B, on='商品ID', how='inner')
                if not comp_df.empty:
                    comp_df['価格差 (A - B)'] = comp_df[f'{store_A}価格'] - comp_df[f'{store_B}価格']
                    st.success(f"**共通アイテムの比較 ({len(comp_df)}件の合致)**")
                    st.dataframe(comp_df[['商品名', f'{store_A}価格', f'{store_B}価格', '価格差 (A - B)']], hide_index=True, use_container_width=True)
                else:
                    st.info("両方の店舗に共通して登録されている商品がありません。")
        else:
            st.info("比較するには2つ以上の販売店データが必要です。")

# ==========================================
# 5. メインコントローラー
# ==========================================
if 'logged_in' not in st.session_state:
    st.session_state['logged_in'] = False

if st.session_state['logged_in']:
    st.sidebar.title("メニュー")
    st.sidebar.write(f"👤 **{st.session_state.get('user_name', 'ゲスト')}** さん")
    
    menu_selection = st.sidebar.radio("機能を選択", ["① 売価チェック", "② 商品マスター管理", "③ 分析ダッシュボード"])
    st.sidebar.markdown("---")
    if st.sidebar.button("ログアウト"):
        st.session_state['logged_in'] = False
        if 'auth_stage' in st.session_state:
            st.session_state['auth_stage'] = 1
        st.rerun()

    if menu_selection == "① 売価チェック":
        price_check_app()
    elif menu_selection == "② 商品マスター管理":
        master_manage_app()
    elif menu_selection == "③ 分析ダッシュボード":
        dashboard_app()
else:
    login_screen()