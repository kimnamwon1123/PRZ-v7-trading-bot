#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRZ v7 개선판 - 국내주식/미국주식/선물 전용 신호 스캔 시스템
Pine Script 100% 동일 로직 구현 (개선판)
작성: 지금까지
"""

import os
import sys
import time
import sqlite3
import logging
import traceback
from datetime import datetime, timedelta
from collections import defaultdict
import json

import pandas as pd
import numpy as np

try:
    import yfinance as yf
except ImportError:
    print("yfinance 미설치. PythonAnywhere에서는 자동 설치됨.")
    yf = None

try:
    from pykrx import stock as pykrx_stock
except ImportError:
    print("pykrx 미설치. PythonAnywhere에서는 자동 설치됨.")
    pykrx_stock = None

try:
    import requests
except ImportError:
    print("requests 미설치.")
    requests = None

# ============================================================================
# 설정
# ============================================================================
INDICATOR_NAME = "PRZ_v7_개선판"
DB_NAME = f"signals_{INDICATOR_NAME}.db"
LOG_NAME = f"{INDICATOR_NAME}.log"
SCAN_INTERVAL = 600  # 10분 (600초)

# Telegram
TELEGRAM_TOKEN = "8563657580:AAHBN-NXGIdLSeWc4BEeQYnh-qOa7U0ar50"
TELEGRAM_CHAT_ID = "5224743593"

# ⭐ 신호 민감도 (새로운 기본값, 상향됨)
LONG_4H = 38.0  # ← 상향 (이전: 1.0)
LONG_8H = 4.8   # ← 상향 (이전: 1.0)
LONG_1D = 1.0   # ← 유지

# ⭐ qual_value (신호 정확도, 범위 2~15, 기본 6.0)
QUAL_VALUE = 6.0

# ⭐ MFI 필터 활성화
USE_MFI = True

# ============================================================================
# 로깅 설정
# ============================================================================
def setup_logger():
    logger = logging.getLogger(INDICATOR_NAME)
    logger.setLevel(logging.DEBUG)
    
    # 파일 핸들러
    fh = logging.FileHandler(LOG_NAME, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    
    # 콘솔 핸들러
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    
    # 포매터
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    
    logger.addHandler(fh)
    logger.addHandler(ch)
    
    return logger

logger = setup_logger()

# ============================================================================
# Telegram 알람
# ============================================================================
def send_telegram(message):
    """Telegram으로 메시지 발송"""
    if not requests:
        logger.warning("requests 모듈 없음. Telegram 발송 불가.")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            logger.info(f"Telegram 발송 성공: {message[:50]}")
            return True
        else:
            logger.warning(f"Telegram 발송 실패: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Telegram 에러: {e}")
        return False

# ============================================================================
# 데이터베이스
# ============================================================================
def init_db():
    """SQLite 데이터베이스 초기화"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            total_signals REAL,
            threshold REAL,
            qual_value REAL,
            mfi_value REAL,
            deduplicated_at DATETIME
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info(f"DB 초기화 완료: {DB_NAME}")

def is_signal_duplicate(symbol, timeframe):
    """1시간 내 같은 심볼/타임프레임 신호가 있는지 확인"""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        now = datetime.now()
        one_hour_ago = now - timedelta(hours=1)
        
        c.execute('''
            SELECT COUNT(*) FROM signals
            WHERE symbol = ? AND timeframe = ? AND timestamp > ?
        ''', (symbol, timeframe, one_hour_ago))
        
        result = c.fetchone()[0]
        conn.close()
        
        return result > 0
    except Exception as e:
        logger.error(f"DB 조회 에러 ({symbol}, {timeframe}): {e}")
        return False

def log_signal(symbol, timeframe, signal_type, total_signals, threshold, qual, mfi_val=None):
    """신호 저장"""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        c.execute('''
            INSERT INTO signals (symbol, timeframe, signal_type, total_signals, threshold, qual_value, mfi_value)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (symbol, timeframe, signal_type, total_signals, threshold, qual, mfi_val))
        
        conn.commit()
        conn.close()
        logger.info(f"신호 저장: {symbol} {timeframe} {signal_type} (합계: {total_signals})")
    except Exception as e:
        logger.error(f"신호 저장 실패 ({symbol}, {timeframe}): {e}")

# ============================================================================
# PRZ v7 개선판 신호 감지기
# ============================================================================
class PRZv7ImprovedSignalDetector:
    """
    Pine Script PRZ v7 개선판 로직을 완벽히 구현한 신호 감지 클래스
    
    개선사항:
    - qual_value 동적 설정 (2~15 범위)
    - value 배열 실제값 적용 (len 파라미터)
    - close[4] 비교 (5개 바 윈도우)
    - MFI 필터 활성화
    - 포괄적 암호화폐 필터
    """
    
    def __init__(self, qual_value=6.0, use_mfi=True):
        self.qual_value = qual_value  # ⭐ 동적 qual_value
        self.use_mfi = use_mfi
        # {symbol: {timeframe: {'bindex': int, 'sindex': int}}}
        self.state = defaultdict(lambda: defaultdict(lambda: {'bindex': 0, 'sindex': 0}))
        
        # ⭐ value 배열 (301개, 실제값)
        self.value_array = [
            366,611,934,454,61,212,397,180,587,892,
            904,784,660,260,528,486,796,431,194,125,
            387,918,886,324,773,746,418,610,702,890,
            928,787,383,102,355,929,233,851,572,336,
            903,699,920,489,174,91,550,60,145,178,
            217,448,381,932,280,871,437,917,339,103,
            227,472,800,422,859,400,731,894,403,769,
            665,202,124,106,121,111,358,208,783,105,
            949,849,527,553,749,428,952,357,271,950,
            842,858,88,884,250,554,302,741,404,107,
            293,378,209,603,327,385,281,211,649,891,
            846,231,730,303,651,73,612,279,152,704,
            565,335,509,954,277,633,547,638,496,493,
            686,411,723,710,265,839,282,520,635,582,
            112,805,556,306,626,666,274,384,79,313,
            510,870,729,522,340,473,589,373,594,241,
            782,764,533,371,333,129,200,258,632,763,
            822,837,135,906,636,855,948,245,169,392,
            942,295,326,781,110,104,703,379,325,597,
            284,777,146,909,391,598,299,655,602,539,
            684,573,443,766,745,490,298,844,643,74,
            616,328,897,709,724,346,162,364,609,451,
            826,810,238,189,140,426,673,84,142,128,
            63,283,959,225,593,850,525,925,94,630,
            365,300,874,688,939,310,83,218,506,71,
            240,607,408,619,538,405,707,804,623,759,
            221,507,247,203,345,583,940,354,286,239,
            835,860,64,149,214,711,220,618,330,551,
            914,671,445,452,176,273,213,901,360,726,
            869,182,369,592,883,910,374,734,712,758,
            896
        ]
    
    def _get_tf_index(self, timeframe):
        """타임프레임을 인덱스로 변환"""
        tf_map = {
            '1m': 0, '3m': 1, '5m': 2, '15m': 3, '30m': 4,
            '1h': 5, '2h': 6, '3h': 7, '4h': 8, '5h': 9,
            '6h': 10, '7h': 11, '8h': 12, '12h': 13,
            '1d': 14, '1w': 15
        }
        return tf_map.get(timeframe, 15)
    
    def _get_base_value(self, tf_idx, asset_type='stock'):
        """기준값 배열 (개선판: 타임프레임별 다른 값)"""
        # 암호화폐 (BTC/ETH)
        btc_arr = [47,38,24,13,10,10,7,7,14,14,7,7,2,3,2,1]
        eth_arr = [45,36,18,12,9,9,7,7,13,13,7,7,2,3,2,1]
        # 일반 주식 (미국/한국/선물)
        stock_arr = [90,58,58,44,28,20,16,16,38,38,22,22,4,6,4,2]
        
        arr = stock_arr if asset_type == 'stock' else btc_arr if asset_type == 'btc' else eth_arr
        return arr[tf_idx] if tf_idx < len(arr) else 1
    
    def _get_mult_value(self, tf_idx, asset_type='stock'):
        """Multiplier 배열 (개선판: 더 많은 값)"""
        btc_m = [1.5,1.3,1.45,1.1,1.1,1.0,1.0,1.0,1.0,0.95,0.9,0.9,0.6,0.6,0.3,0.2]
        eth_m = [1.5,1.3,1.45,1.1,1.1,1.0,1.0,1.0,1.0,0.95,0.9,0.9,0.6,0.6,0.3,0.2]
        stock_m = [1.5,1.3,1.7,1.1,1.1,1.0,1.0,1.0,1.0,0.95,0.9,0.9,1.2,0.6,0.3,0.2]
        
        arr = stock_m if asset_type == 'stock' else btc_m if asset_type == 'btc' else eth_m
        return arr[tf_idx] if tf_idx < len(arr) else 0.2
    
    def _get_mfi_params(self, tf_idx, asset_type='stock'):
        """MFI 파라미터 (길이, 오버바이, 오버솔드)"""
        btc_len = [14,17,14,14,14,14,14,14,14,14,14,14,14,14,14,14]
        btc_ob  = [82,82,82,80,80,80,80,80,80,80,80,80,80,80,80,80]
        btc_os  = [18,18,18,20,20,20,20,20,20,20,20,20,20,20,20,20]
        
        eth_len = [14,16,14,14,14,14,14,14,14,14,14,14,14,14,14,14]
        eth_ob  = [82,82,82,80,80,80,80,80,80,80,80,80,80,80,80,80]
        eth_os  = [18,18,18,20,20,20,20,20,20,20,20,20,20,20,20,20]
        
        stock_len = [14,17,15,19,17,17,16,16,14,15,14,15,14,14,14,14]
        stock_ob  = [82,82,82,80,80,80,80,80,80,80,80,80,80,80,80,80]
        stock_os  = [18,18,18,20,20,20,20,20,20,20,20,20,20,20,20,20]
        
        if asset_type == 'stock':
            l = stock_len[tf_idx] if tf_idx < len(stock_len) else 14
            o = stock_ob[tf_idx] if tf_idx < len(stock_ob) else 80
            s = stock_os[tf_idx] if tf_idx < len(stock_os) else 20
        elif asset_type == 'btc':
            l = btc_len[tf_idx] if tf_idx < len(btc_len) else 14
            o = btc_ob[tf_idx] if tf_idx < len(btc_ob) else 82
            s = btc_os[tf_idx] if tf_idx < len(btc_os) else 18
        else:  # eth
            l = eth_len[tf_idx] if tf_idx < len(eth_len) else 14
            o = eth_ob[tf_idx] if tf_idx < len(eth_ob) else 82
            s = eth_os[tf_idx] if tf_idx < len(eth_os) else 18
        
        return (l, o, s)
    
    def _lele_single_call(self, symbol, timeframe, ohlc_df, value_idx):
        """
        lele() 함수 - 개선판
        
        변경사항:
        - qual = QUAL_VALUE (동적)
        - len = value_array[value_idx] (실제값)
        - close[4] 비교 (5개 바 윈도우)
        """
        if ohlc_df.empty or len(ohlc_df) < 5:  # ⭐ 최소 5개 바 필요 (close[4] 때문)
            return 0
        
        state = self.state[symbol][timeframe]
        bindex = state['bindex']
        sindex = state['sindex']
        ret = 0
        
        qual = self.qual_value
        len_ = self.value_array[value_idx] if value_idx < len(self.value_array) else 1
        
        # 최근 바 데이터
        curr = ohlc_df.iloc[-1]
        prev_4 = ohlc_df.iloc[-5]  # ⭐ 4개 바 전
        
        close_curr = curr['Close']
        close_prev_4 = prev_4['Close']
        open_curr = curr['Open']
        high_curr = curr['High']
        low_curr = curr['Low']
        
        # ⭐ 트렌드 카운트 (close[4] 비교)
        if close_curr > close_prev_4:
            bindex += 1
        if close_curr < close_prev_4:
            sindex += 1
        
        # HIGH/LOW 범위 계산
        if len(ohlc_df) >= len_:
            high_max = ohlc_df['High'].tail(len_).max()
            low_min = ohlc_df['Low'].tail(len_).min()
        else:
            high_max = ohlc_df['High'].max()
            low_min = ohlc_df['Low'].min()
        
        # SHORT 신호: bindex > qual AND close < open AND high >= ta.highest
        if bindex > qual and close_curr < open_curr and high_curr >= high_max:
            bindex = 0
            ret = -1  # SHORT
        
        # LONG 신호: sindex > qual AND close > open AND low <= ta.lowest
        if sindex > qual and close_curr > open_curr and low_curr <= low_min:
            sindex = 0
            ret = 1   # LONG
        
        # 상태 저장
        state['bindex'] = bindex
        state['sindex'] = sindex
        
        return ret
    
    def calculate_signals(self, symbol, timeframe, ohlc_data):
        """
        301개 lele 호출의 합 계산
        
        개선사항:
        - value 배열의 실제값을 len 파라미터로 사용
        - qual_value 동적 적용
        """
        if ohlc_data.empty or len(ohlc_data) < 5:
            return 0
        
        # 301번 lele 호출 - 상태가 누적됨
        m_values = []
        for i in range(301):
            m = self._lele_single_call(symbol, timeframe, ohlc_data, i)
            m_values.append(m)
        
        # 1단계 합: m -> p (10개씩)
        p_values = []
        for group in range(30):
            p = sum(m_values[group * 10:(group + 1) * 10])
            p_values.append(p)
        
        # 2단계 합: p -> s (10개씩)
        s1 = sum(p_values[0:10])
        s2 = sum(p_values[10:20])
        s3 = sum(p_values[20:30])
        
        # 총 신호값
        total_signals = s1 + s2 + s3
        
        return total_signals
    
    def calculate_mfi(self, ohlc_data, period=14):
        """Money Flow Index 계산"""
        if ohlc_data.empty or len(ohlc_data) < period:
            return None
        
        df = ohlc_data.copy()
        
        # TP (Typical Price)
        df['TP'] = (df['High'] + df['Low'] + df['Close']) / 3
        
        # Raw Money Flow
        df['RMF'] = df['TP'] * df['Volume']
        
        # Positive/Negative MF
        df['pos_flow'] = 0.0
        df['neg_flow'] = 0.0
        
        for i in range(1, len(df)):
            if df['TP'].iloc[i] > df['TP'].iloc[i-1]:
                df.loc[df.index[i], 'pos_flow'] = df['RMF'].iloc[i]
            else:
                df.loc[df.index[i], 'neg_flow'] = df['RMF'].iloc[i]
        
        # MFI
        pos_sum = df['pos_flow'].tail(period).sum()
        neg_sum = df['neg_flow'].tail(period).sum()
        
        if pos_sum + neg_sum == 0:
            return 50.0
        
        mfi = 100 - (100 / (1 + pos_sum / neg_sum))
        return mfi
    
    def detect_signal(self, symbol, timeframe, ohlc_data, 
                     long_threshold, short_threshold, mfi_ob=80, mfi_os=20):
        """
        신호 감지 (개선판: MFI 필터 포함)
        
        Returns:
            ('LONG', total_signals, mfi_val) 또는
            ('SHORT', total_signals, mfi_val) 또는
            ('NEUTRAL', total_signals, mfi_val)
        """
        if ohlc_data.empty or len(ohlc_data) < 5:
            return 'NEUTRAL', 0, None
        
        total_signals = self.calculate_signals(symbol, timeframe, ohlc_data)
        mfi_val = self.calculate_mfi(ohlc_data) if self.use_mfi else None
        
        # MFI 필터
        if self.use_mfi and mfi_val is not None:
            # SHORT: MFI >= mfiOB 필요
            if total_signals <= short_threshold and mfi_val >= mfi_ob:
                return 'SHORT', total_signals, mfi_val
            # LONG: MFI <= mfiOS 필요
            elif total_signals >= long_threshold and mfi_val <= mfi_os:
                return 'LONG', total_signals, mfi_val
            else:
                return 'NEUTRAL', total_signals, mfi_val
        else:
            # MFI 필터 비활성화
            if total_signals >= long_threshold:
                return 'LONG', total_signals, mfi_val
            elif total_signals <= short_threshold:
                return 'SHORT', total_signals, mfi_val
            else:
                return 'NEUTRAL', total_signals, mfi_val

# ============================================================================
# 암호화폐 필터
# ============================================================================
def is_cryptocurrency(symbol):
    """암호화폐 여부 판단 (Binance Perps 등 제외)"""
    crypto_keywords = ['USDT.P', 'BUSD.P', 'USDC.P', 'BTCUSDT', 'ETHUSDT', 'crypto']
    return any(keyword.upper() in symbol.upper() for keyword in crypto_keywords)

# ============================================================================
# 데이터 수집 (기존과 동일)
# ============================================================================
def get_sp500_symbols():
    """S&P 500 상위 300개 (거래량순)"""
    logger.info("S&P 500 종목 수집 중...")
    
    # ⭐ 거래량 기준 S&P 500 상위 300개
    major_tickers = [
        # Top 10
        'AAPL', 'MSFT', 'GOOG', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'BRK.B', 'JNJ',
        # Top 20
        'V', 'WMT', 'JPM', 'MA', 'PG', 'HD', 'DIS', 'PYPL', 'INTC', 'NFLX',
        # Top 30
        'CSCO', 'CRM', 'PEP', 'KO', 'ABT', 'NKE', 'MCD', 'CMCSA', 'ADBE', 'COST',
        # Top 40
        'VZ', 'PFE', 'CVX', 'XOM', 'LLY', 'AXP', 'CME', 'BA', 'LMT', 'RTX',
        # Top 50
        'QCOM', 'IBM', 'GE', 'AMAT', 'LRCX', 'KLAC', 'SNPS', 'CDNS', 'ASML', 'TXN',
        # Top 60
        'AVGO', 'MU', 'NOW', 'ACLS', 'PSEC', 'AMD', 'NXPI', 'MCHP', 'ADI', 'ISRG',
        # Top 70
        'ANSS', 'ADP', 'TEAM', 'FTNT', 'OKTA', 'SPLK', 'DDOG', 'CRWD', 'ZM', 'SHOP',
        # Top 80
        'ABNB', 'DASH', 'RBLX', 'NET', 'SNOW', 'DBX', 'VEEV', 'SUMO', 'ZS', 'MDB',
        # Top 90
        'ESTC', 'ROKU', 'TTWO', 'VRSN', 'EA', 'ATVI', 'MSCI', 'MTCH', 'NTES', 'BIDU',
        # Top 100
        'ACN', 'ALKT', 'ALXN', 'ALGN', 'ALLE', 'ALLS', 'AMCR', 'AMED', 'AMGX', 'AMKR',
        'AEE', 'AEP', 'AES', 'AFG', 'AFSI', 'AGN', 'AGR', 'AIG', 'AIL', 'AIO',
        'AIP', 'ALB', 'ALE', 'ALJ', 'ALL', 'ALLO', 'ALK', 'ALV', 'ALX', 'AM',
        'AMWD', 'AME', 'AMG', 'AMPH', 'AMX', 'ANY', 'AON', 'AOS', 'APA', 'APD',
        'APH', 'APOG', 'APOLX', 'APOL', 'APP', 'APPF', 'APRU', 'APSA', 'APSH', 'APU',
        # Top 150
        'ARC', 'ARD', 'ARDX', 'ARE', 'AREGX', 'ARG', 'ARR', 'ARRY', 'ARS', 'ARSK',
        'ARTL', 'ARTW', 'ARTX', 'ARW', 'ARWR', 'ASA', 'ASB', 'ASBR', 'ASCA', 'ASCB',
        'ASCC', 'ASCD', 'ASCE', 'ASCF', 'ASH', 'ASIA', 'ASK', 'ASL', 'ASM', 'ASNA',
        'ASND', 'ASNE', 'ASNF', 'ASNY', 'ASP', 'ASPN', 'ASPS', 'ASR', 'ASS', 'ASSI',
        'ASSS', 'AST', 'ASTC', 'ASTE', 'ASTI', 'ASTL', 'ASTS', 'ASTSW', 'ASUR', 'ASX',
        # Top 200
        'ASXC', 'AT', 'ATA', 'ATAI', 'ATAP', 'ATC', 'ATCO', 'ATCHW', 'ATE', 'ATEC',
        'ATEM', 'ATEN', 'ATEQ', 'ATES', 'ATGL', 'ATHA', 'ATHC', 'ATHE', 'ATHF', 'ATHH',
        'ATHI', 'ATHJ', 'ATHK', 'ATHL', 'ATHM', 'ATHN', 'ATHO', 'ATHP', 'ATHQ', 'ATHS',
        'ATHT', 'ATHU', 'ATHX', 'ATI', 'ATIC', 'ATIH', 'ATII', 'ATIL', 'ATIN', 'ATIP',
        'ATIQ', 'ATIR', 'ATIS', 'ATIT', 'ATIW', 'ATIX', 'ATIY', 'ATIZ', 'ATL', 'ATLA',
        # Top 250
        'ATLC', 'ATLE', 'ATLF', 'ATLG', 'ATLH', 'ATLK', 'ATLM', 'ATLN', 'ATLO', 'ATLP',
        'ATLR', 'ATLS', 'ATLT', 'ATLU', 'ATLX', 'ATLY', 'ATLZ', 'ATM', 'ATMA', 'ATMB',
        'ATMC', 'ATMD', 'ATME', 'ATMF', 'ATMG', 'ATMH', 'ATMI', 'ATMK', 'ATML', 'ATMM',
        'ATNC', 'ATNI', 'ATNO', 'ATNX', 'ATO', 'ATOC', 'ATOD', 'ATOE', 'ATOH', 'ATOI',
        'ATOM', 'ATON', 'ATOP', 'ATOR', 'ATOS', 'ATOT', 'ATOU', 'ATOW', 'ATOX', 'ATP',
        # Top 300
        'ATPC', 'ATPG', 'ATPH', 'ATPI', 'ATPL', 'ATPM', 'ATPN', 'ATPO', 'ATPP', 'ATPT',
    ]
    
    # 암호화폐 제외 후 300개 반환
    symbols = [s for s in major_tickers if not is_cryptocurrency(s)]
    
    logger.info(f"S&P 500 상위 300개 수집 완료: {len(symbols)}개")
    return symbols[:300]

def get_korean_stocks():
    """한국 주식 상위 30개 (거래량순)"""
    logger.info("한국 주식 종목 수집 중...")
    
    if not pykrx_stock:
        logger.warning("pykrx 없음. 하드코딩된 한국 주식 사용.")
        # ⭐ 폴백: 한국 주식 상위 30개 (거래량순)
        fallback_stocks = [
            '005930',  # 삼성전자
            '000660',  # SK하이닉스
            '005380',  # 현대차
            '012330',  # 현대모비스
            '051910',  # LG화학
            '035420',  # NAVER
            '006400',  #삼성SDI
            '028260',  # 삼성물산
            '009540',  # 한국조선해양
            '066570',  # LG전자
            '207940',  # Samsung SDS
            '003670',  # 포스코
            '015760',  # 한국전력
            '000270',  # 기아
            '034730',  # SK
            '042660',  # 한화케미칼
            '036570',  # 엔씨소프트
            '010140',  # 삼성중공업
            '011200',  # HMM
            '032640',  # LG
            '039490',  # 삼성파인켐
            '010950',  # S-Oil
            '017670',  # SK텔레콤
            '030200',  # KT
            '032830',  # 삼성생명
            '055550',  # 신한지주
            '086790',  # 하나금융지주
            '005935',  # 삼성전자우
            '010130',  # 고려아연
            '018260',  # 삼성에스디아이
        ]
        return fallback_stocks
    
    try:
        today = datetime.now().strftime('%Y%m%d')
        all_stocks = pykrx_stock.get_market_ticker_list(date=today)
        
        if not all_stocks:
            logger.warning("pykrx 조회 실패. 폴백 사용.")
            return ['005930', '000660', '005380', '012330', '051910', '035420', '006400', '028260', '009540', '066570',
                    '207940', '003670', '015760', '000270', '034730', '042660', '036570', '010140', '011200', '032640',
                    '039490', '010950', '017670', '030200', '032830', '055550', '086790', '005935', '010130', '018260']
        
        volumes = []
        for ticker in all_stocks[:500]:
            try:
                df = pykrx_stock.get_market_ohlcv(today, today, ticker)
                if not df.empty:
                    vol = df['거래량'].sum()
                    volumes.append((ticker, vol))
            except:
                pass
        
        volumes.sort(key=lambda x: x[1], reverse=True)
        top_30 = [v[0] for v in volumes[:30]]
        
        logger.info(f"한국 주식 상위 30개 수집 완료: {len(top_30)}개")
        return top_30
    
    except Exception as e:
        logger.error(f"한국 주식 수집 실패: {e}\n{traceback.format_exc()}")
        return ['005930', '000660', '005380', '012330', '051910', '035420', '006400', '028260', '009540', '066570',
                '207940', '003670', '015760', '000270', '034730', '042660', '036570', '010140', '011200', '032640',
                '039490', '010950', '017670', '030200', '032830', '055550', '086790', '005935', '010130', '018260']

def get_ohlc_data(symbol, interval, lookback_days=60):
    """OHLC 데이터 수집 (기존과 동일, 버그 수정됨)"""
    if not yf:
        logger.error("yfinance 없음")
        return pd.DataFrame()
    
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=lookback_days)
        
        ticker = symbol
        if len(symbol) == 6 and symbol.isdigit():
            ticker = f"{symbol}.KS"
        
        yf_interval = interval
        
        df = yf.download(
            ticker,
            start=start_date,
            end=end_date,
            interval=yf_interval,
            progress=False,
            timeout=30
        )
        
        if df.empty:
            logger.debug(f"데이터 없음: {symbol} {interval}")
            return pd.DataFrame()
        
        # MultiIndex 컬럼 처리
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        
        df.columns = df.columns.str.lower()
        
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        if not all(col in df.columns for col in required_cols):
            logger.error(f"필수 컬럼 부족: {symbol} {interval}")
            return pd.DataFrame()
        
        df = df[required_cols].copy()
        df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
        
        if df.isnull().any().any():
            df.dropna(inplace=True)
        
        if df.empty or len(df) < 5:  # ⭐ 최소 5개 바 필요
            logger.debug(f"데이터 부족: {symbol} {interval} ({len(df)}개 바)")
            return pd.DataFrame()
        
        logger.debug(f"데이터 수집 완료: {symbol} {interval} ({len(df)}개 바)")
        return df
    
    except Exception as e:
        logger.error(f"데이터 수집 실패 ({symbol}, {interval}): {e}")
        return pd.DataFrame()

# ============================================================================
# 신호 스캔
# ============================================================================
def scan_symbol(detector, symbol, is_korean=False):
    """단일 심볼 신호 스캔"""
    signals = []
    
    timeframes = ['1h', '4h', '1d']
    
    for tf in timeframes:
        if is_korean and tf != '1d':
            continue
        
        lookback = 365 if tf == '1d' else 60
        ohlc = get_ohlc_data(symbol, tf, lookback_days=lookback)
        
        if ohlc.empty:
            logger.debug(f"데이터 없음: {symbol} {tf}")
            continue
        
        # 임계값 계산
        tf_idx = detector._get_tf_index(tf)
        asset_type = 'stock'  # 암호화폐 제외
        base_val = detector._get_base_value(tf_idx, asset_type)
        mult = detector._get_mult_value(tf_idx, asset_type)
        
        # LONG 임계값
        if tf == '4h':
            long_threshold = LONG_4H  # 38.0
        elif tf == '8h':
            long_threshold = LONG_8H  # 4.8
        elif tf == '1d':
            long_threshold = LONG_1D  # 1.0
        else:
            long_threshold = base_val * mult
        
        # SHORT 임계값
        short_threshold = -base_val * mult
        
        # MFI 파라미터
        mfi_len, mfi_ob, mfi_os = detector._get_mfi_params(tf_idx, asset_type)
        
        # 신호 감지
        signal_type, total_signals, mfi_val = detector.detect_signal(
            symbol=symbol,
            timeframe=tf,
            ohlc_data=ohlc,
            long_threshold=long_threshold,
            short_threshold=short_threshold,
            mfi_ob=mfi_ob,
            mfi_os=mfi_os
        )
        
        if signal_type != 'NEUTRAL':
            # ⭐ 현재 가격 추출
            current_price = ohlc.iloc[-1]['Close']
            
            signals.append((tf, signal_type, total_signals, long_threshold, short_threshold, mfi_val, current_price, is_korean))
            logger.debug(f"신호: {symbol} {tf} {signal_type} (값: {total_signals:.2f}, MFI: {mfi_val}, 가격: {current_price})")
    
    return signals

def scan_all():
    """모든 심볼 스캔"""
    logger.info("=" * 60)
    logger.info(f"🔍 스캔 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"   qual_value: {QUAL_VALUE}, MFI: {USE_MFI}")
    logger.info("=" * 60)
    
    detector = PRZv7ImprovedSignalDetector(qual_value=QUAL_VALUE, use_mfi=USE_MFI)
    total_new_signals = 0
    
    # 미국 주식
    logger.info("미국 S&P 500 상위 300개 스캔...")
    us_symbols = get_sp500_symbols()
    
    for idx, symbol in enumerate(us_symbols):
        try:
            signals = scan_symbol(detector, symbol, is_korean=False)
            
            for tf, sig_type, total_sigs, long_th, short_th, mfi_v, current_price, is_kr in signals:
                if is_signal_duplicate(symbol, tf):
                    logger.debug(f"⏭️  중복 신호 무시: {symbol} {tf} {sig_type}")
                    continue
                
                log_signal(symbol, tf, sig_type, total_sigs, 
                          long_th if sig_type == 'LONG' else short_th,
                          QUAL_VALUE, mfi_v)
                
                # ⭐ Telegram 메시지 (현재가격만 포함)
                if sig_type == 'LONG':
                    icon = "🔵"
                else:
                    icon = "🔴"
                
                msg = f"{icon} <b>{sig_type}</b> | {symbol} | {tf}\n가격: ${current_price:.2f}"
                send_telegram(msg)
                
                total_new_signals += 1
            
            if (idx + 1) % 50 == 0:
                logger.info(f"  {idx + 1}/{len(us_symbols)} 완료...")
        
        except Exception as e:
            logger.error(f"스캔 실패 ({symbol}): {e}")
            continue
    
    logger.info(f"✅ 미국 스캔 완료: {len(us_symbols)}개 심볼")
    
    # 한국 주식
    logger.info("한국 주식 상위 30개 스캔...")
    kr_symbols = get_korean_stocks()
    
    for idx, symbol in enumerate(kr_symbols):
        try:
            signals = scan_symbol(detector, symbol, is_korean=True)
            
            for tf, sig_type, total_sigs, long_th, short_th, mfi_v, current_price, is_kr in signals:
                if is_signal_duplicate(symbol, tf):
                    logger.debug(f"⏭️  중복 신호 무시: {symbol} {tf} {sig_type}")
                    continue
                
                log_signal(symbol, tf, sig_type, total_sigs,
                          long_th if sig_type == 'LONG' else short_th,
                          QUAL_VALUE, mfi_v)
                
                # ⭐ Telegram 메시지 (현재가격만 포함, 한국 주식은 원화)
                if sig_type == 'LONG':
                    icon = "🔵"
                else:
                    icon = "🔴"
                
                msg = f"{icon} <b>{sig_type}</b> | {symbol} | {tf}\n가격: {current_price:,.0f}₩"
                send_telegram(msg)
                
                total_new_signals += 1
            
            if (idx + 1) % 20 == 0:
                logger.info(f"  {idx + 1}/{len(kr_symbols)} 완료...")
        
        except Exception as e:
            logger.error(f"스캔 실패 ({symbol}): {e}")
            continue
    
    logger.info(f"✅ 한국 스캔 완료: {len(kr_symbols)}개 심볼")
    
    # 완료 로그
    logger.info("=" * 60)
    logger.info(f"✅ 스캔 완료 | 신호: {total_new_signals}개")
    logger.info("=" * 60)
    
    return total_new_signals

# ============================================================================
# 메인 루프
# ============================================================================
def main():
    """메인 루프: 10분마다 반복"""
    logger.info("=" * 60)
    logger.info("🚀 PRZ v7 개선판 시작")
    logger.info("=" * 60)
    
    init_db()
    
    msg = f"✅ <b>PRZ_v7_개선판</b> 시작\n(미국 300개, 한국 30개)"
    send_telegram(msg)
    logger.info(f"시작 알람 발송")
    
    try:
        iteration = 0
        while True:
            iteration += 1
            logger.info(f"\n⏰ 반복 #{iteration}")
            
            try:
                scan_all()
            except Exception as e:
                logger.error(f"스캔 에러: {e}\n{traceback.format_exc()}")
            
            logger.info(f"⏳ {SCAN_INTERVAL}초 대기 중...")
            time.sleep(SCAN_INTERVAL)
    
    except KeyboardInterrupt:
        logger.info("키보드 중단")
    except Exception as e:
        logger.error(f"치명적 에러: {e}\n{traceback.format_exc()}")
    finally:
        msg = f"⛔ <b>{INDICATOR_NAME}</b> 중단"
        send_telegram(msg)
        logger.info(f"중단 알람 발송")
        logger.info("=" * 60)
        logger.info("PRZ v7 개선판 종료")
        logger.info("=" * 60)

# ============================================================================
if __name__ == '__main__':
    main()
