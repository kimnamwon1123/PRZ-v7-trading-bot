#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRZ v7 개선판 - 국내주식/미국주식/선물 전용 신호 스캔 시스템
Pine Script 100% 동일 로직 구현 (개선판 + Telegram 포맷 개선 + 캔들 타이밍 수정)
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
import pytz

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
# Telegram 알람 (개선판: 상세 포맷)
# ============================================================================
def send_telegram(symbol, timeframe, signal_type, current_price, total_signals, 
                  qual_value, mfi_val, is_korean=False):
    """
    Telegram 신호 알람 발송 (개선판)
    
    TradingView 스타일의 상세 메시지 포맷
    """
    if not requests:
        logger.warning("requests 모듈 없음. Telegram 발송 불가.")
        return False
    
    try:
        # 시장 구분
        market = "[한국주식]" if is_korean else "[미국주식]"
        
        # 신호 타입 표시
        if signal_type == 'LONG':
            signal_str = "🔵 PRZ 롱(매수) 시그널"
        else:
            signal_str = "🔴 PRZ 숏(매도) 시그널"
        
        # 타임프레임 표시 (한글)
        tf_kr = {
            '1h': '1시간봉',
            '4h': '4시간봉',
            '8h': '8시간봉',
            '1d': '일봉'
        }
        timeframe_str = tf_kr.get(timeframe, timeframe)
        
        # 시간 표시
        current_time = datetime.now().strftime('%Y-%m-%d')
        
        # 가격 포맷
        if is_korean:
            price_str = f"{current_price:,.0f}₩"
        else:
            price_str = f"${current_price:.2f}"
        
        # 메시지 구성 (간단한 포맷)
        message = f"""{market} {signal_str}
종목: {symbol}
타임프레임: {timeframe_str}
가격: {price_str}
시각: {current_time}"""
        
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            logger.info(f"✅ Telegram 발송: {symbol} {timeframe} {signal_type}")
            return True
        else:
            logger.warning(f"❌ Telegram 발송 실패: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Telegram 에러: {e}")
        return False

# ============================================================================
# 데이터베이스 (개선: 신호 상태 추적)
# ============================================================================
def init_db():
    """SQLite 데이터베이스 초기화 (신호 상태 추적 추가)"""
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
            candle_close_time DATETIME,
            deduplicated_at DATETIME
        )
    ''')
    
    # 신호 상태 추적 테이블 (매우 중요!)
    c.execute('''
        CREATE TABLE IF NOT EXISTS signal_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            last_signal_candle_time DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, timeframe, signal_type)
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info(f"✅ DB 초기화 완료: {DB_NAME}")

def get_last_signal_time(symbol, timeframe, signal_type):
    """
    마지막 신호 시간 조회
    
    같은 신호가 반복되지 않도록 추적
    """
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        c.execute('''
            SELECT last_signal_candle_time FROM signal_status
            WHERE symbol = ? AND timeframe = ? AND signal_type = ?
        ''', (symbol, timeframe, signal_type))
        
        result = c.fetchone()
        conn.close()
        
        if result and result[0]:
            return datetime.fromisoformat(result[0])
        return None
    except Exception as e:
        logger.error(f"DB 조회 에러 ({symbol}, {timeframe}): {e}")
        return None

def update_signal_status(symbol, timeframe, signal_type, candle_time):
    """
    신호 상태 업데이트
    
    매우 중요: 이전 캔들의 신호를 추적
    """
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        c.execute('''
            INSERT OR REPLACE INTO signal_status 
            (symbol, timeframe, signal_type, last_signal_candle_time, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (symbol, timeframe, signal_type, candle_time))
        
        conn.commit()
        conn.close()
        logger.debug(f"✅ 신호 상태 업데이트: {symbol} {timeframe} {signal_type}")
    except Exception as e:
        logger.error(f"신호 상태 저장 실패: {e}")

def log_signal(symbol, timeframe, signal_type, total_signals, threshold, qual, 
               mfi_val=None, candle_close_time=None):
    """신호 저장"""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        
        c.execute('''
            INSERT INTO signals 
            (symbol, timeframe, signal_type, total_signals, threshold, qual_value, mfi_value, candle_close_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (symbol, timeframe, signal_type, total_signals, threshold, qual, mfi_val, candle_close_time))
        
        conn.commit()
        conn.close()
        logger.info(f"✅ 신호 저장: {symbol} {timeframe} {signal_type}")
    except Exception as e:
        logger.error(f"신호 저장 실패 ({symbol}, {timeframe}): {e}")

# ============================================================================
# PRZ v7 개선판 신호 감지기 (Pine Script 100% 동일)
# ============================================================================
class PRZv7ImprovedSignalDetector:
    """
    Pine Script PRZ v7 개선판 로직을 100% 동일하게 구현
    
    ⭐ 핵심 수정:
    - 301개 lele 인스턴스 각각 독립된 bindex/sindex 보유
    - 모든 과거 바를 처음부터 순서대로 처리 (Pine Script와 동일)
    - 마지막 바의 결과만 신호값으로 사용
    """
    
    def __init__(self, qual_value=6.0, use_mfi=True):
        self.qual_value = qual_value
        self.use_mfi = use_mfi
        
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
        tf_map = {
            '1m': 0, '3m': 1, '5m': 2, '15m': 3, '30m': 4,
            '1h': 5, '2h': 6, '3h': 7, '4h': 8, '5h': 9,
            '6h': 10, '7h': 11, '8h': 12, '12h': 13,
            '1d': 14, '1w': 15
        }
        return tf_map.get(timeframe, 15)
    
    def _get_base_value(self, tf_idx, asset_type='stock'):
        stock_arr = [90,58,58,44,28,20,16,16,38,38,22,22,4,6,4,2]
        return stock_arr[tf_idx] if tf_idx < len(stock_arr) else 1
    
    def _get_mult_value(self, tf_idx, asset_type='stock'):
        stock_m = [1.5,1.3,1.7,1.1,1.1,1.0,1.0,1.0,1.0,0.95,0.9,0.9,1.2,0.6,0.3,0.2]
        return stock_m[tf_idx] if tf_idx < len(stock_m) else 0.2
    
    def _get_mfi_params(self, tf_idx, asset_type='stock'):
        stock_len = [14,17,15,19,17,17,16,16,14,15,14,15,14,14,14,14]
        stock_ob  = [82,82,82,80,80,80,80,80,80,80,80,80,80,80,80,80]
        stock_os  = [18,18,18,20,20,20,20,20,20,20,20,20,20,20,20,20]
        l = stock_len[tf_idx] if tf_idx < len(stock_len) else 14
        o = stock_ob[tf_idx] if tf_idx < len(stock_ob) else 80
        s = stock_os[tf_idx] if tf_idx < len(stock_os) else 20
        return (l, o, s)
    
    def calculate_signals(self, symbol, timeframe, ohlc_data):
        """
        ⭐ Pine Script와 100% 동일한 방식으로 신호 계산
        
        Pine Script 동작:
        1. 301개 lele 인스턴스, 각각 독립된 var int bindex, sindex
        2. 모든 바에서 301개 인스턴스 전부 실행
        3. 마지막 바의 301개 결과를 합산 = total_signals_really
        
        Python 구현:
        1. bindex[301], sindex[301] 배열로 독립 상태 관리
        2. 모든 과거 바를 처음부터 순서대로 처리
        3. 마지막 바의 합산값 반환
        """
        if ohlc_data.empty or len(ohlc_data) < 5:
            return 0
        
        # numpy 배열로 변환 (속도 최적화)
        closes = ohlc_data['Close'].values.astype(float)
        opens = ohlc_data['Open'].values.astype(float)
        highs = ohlc_data['High'].values.astype(float)
        lows = ohlc_data['Low'].values.astype(float)
        n_bars = len(ohlc_data)
        
        qual = self.qual_value
        
        # ⭐ 301개 인스턴스의 독립된 bindex/sindex
        bindex = np.zeros(301, dtype=np.float64)
        sindex = np.zeros(301, dtype=np.float64)
        
        total_signals = 0  # 마지막 바의 합계
        
        # ⭐ 모든 바를 순서대로 처리 (Pine Script와 동일)
        for bar_idx in range(4, n_bars):  # close[4] 때문에 4부터
            close_curr = closes[bar_idx]
            close_prev4 = closes[bar_idx - 4]
            open_curr = opens[bar_idx]
            high_curr = highs[bar_idx]
            low_curr = lows[bar_idx]
            
            # ⭐ 트렌드 카운트 (모든 301개 인스턴스 동일하게 증가)
            if close_curr > close_prev4:
                bindex += 1  # numpy 벡터 연산
            if close_curr < close_prev4:
                sindex += 1  # numpy 벡터 연산
            
            # ⭐ 각 인스턴스별 신호 조건 확인
            bar_sum = 0
            for i in range(301):
                len_ = self.value_array[i]
                ret = 0
                
                # ta.highest(high, len) / ta.lowest(low, len)
                start = max(0, bar_idx - len_ + 1)
                high_max = highs[start:bar_idx + 1].max()
                low_min = lows[start:bar_idx + 1].min()
                
                # SHORT: bindex > qual AND close < open AND high >= highest
                if bindex[i] > qual and close_curr < open_curr and high_curr >= high_max:
                    bindex[i] = 0  # 개별 인스턴스만 리셋
                    ret = -1
                
                # LONG: sindex > qual AND close > open AND low <= lowest
                if sindex[i] > qual and close_curr > open_curr and low_curr <= low_min:
                    sindex[i] = 0  # 개별 인스턴스만 리셋
                    ret = 1
                
                bar_sum += ret
            
            total_signals = bar_sum  # 매 바마다 갱신, 마지막 바가 최종값
        
        logger.debug(f"📊 {symbol} {timeframe}: 신호값 = {total_signals} ({n_bars}개 바 처리)")
        return total_signals
    
    def calculate_mfi(self, ohlc_data, period=14):
        """Money Flow Index 계산"""
        if ohlc_data.empty or len(ohlc_data) < period:
            return None
        
        df = ohlc_data.copy()
        
        df['TP'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['RMF'] = df['TP'] * df['Volume']
        
        df['pos_flow'] = 0.0
        df['neg_flow'] = 0.0
        
        for i in range(1, len(df)):
            if df['TP'].iloc[i] > df['TP'].iloc[i-1]:
                df.loc[df.index[i], 'pos_flow'] = df['RMF'].iloc[i]
            else:
                df.loc[df.index[i], 'neg_flow'] = df['RMF'].iloc[i]
        
        pos_sum = df['pos_flow'].tail(period).sum()
        neg_sum = df['neg_flow'].tail(period).sum()
        
        if pos_sum + neg_sum == 0:
            return 50.0
        
        mfi = 100 - (100 / (1 + pos_sum / neg_sum))
        return mfi
    
    def detect_signal(self, symbol, timeframe, ohlc_data, 
                     long_threshold, short_threshold, mfi_ob=80, mfi_os=20):
        """신호 감지 (MFI 필터 포함)"""
        if ohlc_data.empty or len(ohlc_data) < 5:
            return 'NEUTRAL', 0, None
        
        total_signals = self.calculate_signals(symbol, timeframe, ohlc_data)
        mfi_val = self.calculate_mfi(ohlc_data) if self.use_mfi else None
        
        if self.use_mfi and mfi_val is not None:
            if total_signals <= short_threshold and mfi_val >= mfi_ob:
                return 'SHORT', total_signals, mfi_val
            elif total_signals >= long_threshold and mfi_val <= mfi_os:
                return 'LONG', total_signals, mfi_val
            else:
                return 'NEUTRAL', total_signals, mfi_val
        else:
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
    """암호화폐 여부 판단"""
    crypto_keywords = ['USDT.P', 'BUSD.P', 'USDC.P', 'BTCUSDT', 'ETHUSDT', 'crypto']
    return any(keyword.upper() in symbol.upper() for keyword in crypto_keywords)

# ============================================================================
# 데이터 수집
# ============================================================================
def get_sp500_symbols():
    """S&P 500 실제 상장 종목 300개"""
    major_tickers = [
        # Mega Cap
        'AAPL', 'MSFT', 'GOOG', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'BRK-B', 'JNJ',
        'V', 'WMT', 'JPM', 'MA', 'PG', 'HD', 'DIS', 'PYPL', 'INTC', 'NFLX',
        # Large Cap Tech
        'CSCO', 'CRM', 'PEP', 'KO', 'ABT', 'NKE', 'MCD', 'CMCSA', 'ADBE', 'COST',
        'VZ', 'PFE', 'CVX', 'XOM', 'LLY', 'AXP', 'CME', 'BA', 'LMT', 'RTX',
        'QCOM', 'IBM', 'GE', 'AMAT', 'LRCX', 'KLAC', 'SNPS', 'CDNS', 'ASML', 'TXN',
        'AVGO', 'MU', 'NOW', 'AMD', 'NXPI', 'MCHP', 'ADI', 'ISRG',
        'ADP', 'TEAM', 'FTNT', 'DDOG', 'CRWD', 'ZM', 'SHOP',
        'ABNB', 'DASH', 'RBLX', 'NET', 'SNOW', 'DBX', 'VEEV', 'ZS', 'MDB',
        'ROKU', 'TTWO', 'VRSN', 'EA', 'MSCI', 'MTCH', 'NTES', 'BIDU',
        # S&P 500 Large/Mid Cap
        'ACN', 'ALGN', 'AMCR', 'AMKR', 'AEE', 'AEP', 'AES', 'AIG',
        'ALB', 'ALL', 'ALK', 'AME', 'AON', 'AOS', 'APA', 'APD',
        'APH', 'APP', 'ARE', 'ARW', 'ASB', 'ASH',
        'ASND', 'ASPN', 'ASPS', 'ASTS', 'ATEN', 'ATI',
        'ATO', 'ATNI', 'ATOS',
        # Financial
        'BAC', 'BK', 'BLK', 'C', 'CB', 'CFG', 'CINF', 'CMA', 'COF', 'DFS',
        'FDS', 'FITB', 'FRC', 'GL', 'GS', 'HBAN', 'HIG', 'ICE', 'IVZ',
        'KEY', 'L', 'MET', 'MKTX', 'MMC', 'MS', 'NTRS', 'PNC', 'PRU',
        'RE', 'RF', 'SCHW', 'SIVB', 'STT', 'SYF', 'TFC', 'TROW', 'TRV', 'USB', 'WFC', 'ZION',
        # Healthcare
        'ABBV', 'AMGN', 'BAX', 'BDX', 'BIIB', 'BMY', 'BSX', 'CAH', 'CI', 'CNC',
        'CRL', 'CTLT', 'CVS', 'DGX', 'DHR', 'DVA', 'ELV', 'EW', 'GILD', 'HCA',
        'HOLX', 'HSIC', 'IDXX', 'ILMN', 'INCY', 'IQV', 'MCK', 'MDT', 'MOH', 'MRK',
        'MTD', 'PKI', 'REGN', 'RMD', 'STE', 'SYK', 'TMO', 'UNH', 'VRTX', 'WAT', 'WST', 'ZBH', 'ZTS',
        # Industrial
        'CAT', 'CTAS', 'DE', 'DOV', 'EMR', 'ETN', 'FDX', 'GD', 'GPC', 'GWW',
        'HII', 'HON', 'HWM', 'IEX', 'IR', 'ITW', 'J', 'JCI', 'LHX', 'MMM',
        'NOC', 'NSC', 'ODFL', 'OTIS', 'PCAR', 'PH', 'PNR', 'PWR', 'ROK', 'ROL',
        'RSG', 'SNA', 'SWK', 'TDG', 'TXT', 'UNP', 'UPS', 'URI', 'WAB', 'WM', 'XYL',
        # Consumer
        'AMZN', 'AZO', 'BBY', 'BKNG', 'BWA', 'CCL', 'CMG', 'CZR', 'DAL', 'DG',
        'DHI', 'DLTR', 'DPZ', 'DRI', 'EBAY', 'ETSY', 'EXPE', 'F', 'GM', 'GRMN',
        'HAS', 'HLT', 'KMX', 'LEN', 'LOW', 'LVS', 'MAR', 'MGM', 'MHK', 'MNST',
        'NVR', 'NWL', 'ORLY', 'PHM', 'POOL', 'RL', 'RCL', 'SBUX', 'TGT', 'TSCO',
        'TPR', 'ULTA', 'VFC', 'WHR', 'WYNN', 'YUM',
        # Energy
        'APC', 'BKR', 'COP', 'CTRA', 'DVN', 'EOG', 'FANG', 'HAL', 'HES', 'KMI',
        'MPC', 'MRO', 'OKE', 'OXY', 'PSX', 'PXD', 'SLB', 'TRGP', 'VLO', 'WMB',
        # Utilities / Real Estate
        'AMT', 'AWK', 'CCI', 'D', 'DLR', 'DTE', 'DUK', 'ED', 'EIX', 'ES',
        'EVRG', 'EXC', 'FE', 'NEE', 'NI', 'O', 'PEG', 'PPL', 'PSA', 'REG',
        'SBAC', 'SO', 'SRE', 'WEC', 'XEL',
    ]
    
    # 중복 제거
    seen = set()
    unique_tickers = []
    for t in major_tickers:
        if t not in seen:
            seen.add(t)
            unique_tickers.append(t)
    
    symbols = [s for s in unique_tickers if not is_cryptocurrency(s)]
    logger.info(f"📊 S&P 500 {len(symbols)}개 로드 완료")
    return symbols

def get_korean_stocks():
    """한국 주식 상위 50개 (거래량 높은 순)"""
    logger.info("📊 한국 주식 종목 수집 중...")
    
    # ⭐ 거래량 상위 50개 (기존 30 + 추가 20)
    fallback_stocks = [
        # 기존 30개 (시가총액/거래량 상위)
        '005930',  # 삼성전자
        '000660',  # SK하이닉스
        '005380',  # 현대차
        '012330',  # 현대모비스
        '051910',  # LG화학
        '035420',  # NAVER
        '006400',  # 삼성SDI
        '028260',  # 삼성물산
        '009540',  # 한국조선해양
        '066570',  # LG전자
        '207940',  # 삼성바이오로직스
        '003670',  # 포스코홀딩스
        '015760',  # 한국전력
        '000270',  # 기아
        '034730',  # SK
        '042660',  # 한화오션
        '036570',  # 엔씨소프트
        '010140',  # 삼성중공업
        '011200',  # HMM
        '032640',  # LG유플러스
        '039490',  # 키움증권
        '010950',  # S-Oil
        '017670',  # SK텔레콤
        '030200',  # KT
        '032830',  # 삼성생명
        '055550',  # 신한지주
        '086790',  # 하나금융지주
        '005935',  # 삼성전자우
        '010130',  # 고려아연
        '018260',  # 삼성에스디에스
        # ⭐ 추가 20개 (거래량 상위)
        '034220',  # LG디스플레이
        '035720',  # 카카오
        '068270',  # 셀트리온
        '373220',  # LG에너지솔루션
        '003490',  # 대한항공
        '096770',  # SK이노베이션
        '047050',  # 포스코인터내셔널
        '000810',  # 삼성화재
        '033780',  # KT&G
        '009150',  # 삼성전기
        '316140',  # 우리금융지주
        '105560',  # KB금융
        '034020',  # 두산에너빌리티
        '402340',  # SK스퀘어
        '352820',  # 하이브
        '018880',  # 한온시스템
        '010620',  # 현대미포조선
        '006360',  # GS건설
        '000990',  # DB하이텍
        '093370',  # 후성
    ]
    
    if not pykrx_stock:
        logger.info(f"📊 한국 주식 폴백 {len(fallback_stocks)}개 사용")
        return fallback_stocks
    
    try:
        today = datetime.now().strftime('%Y%m%d')
        all_stocks = pykrx_stock.get_market_ticker_list(date=today)
        
        if not all_stocks:
            logger.warning("⚠️  pykrx 조회 실패. 폴백 사용.")
            return fallback_stocks
        
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
        top_50 = [v[0] for v in volumes[:50]]
        
        logger.info(f"✅ 한국 주식 상위 50개 수집 완료: {len(top_50)}개")
        return top_50
    
    except Exception as e:
        logger.error(f"❌ 한국 주식 수집 실패: {e}")
        return fallback_stocks

def get_tf_minutes(interval):
    """타임프레임을 분 단위로 변환"""
    tf_map = {
        '1m': 1, '3m': 3, '5m': 5, '15m': 15, '30m': 30,
        '1h': 60, '2h': 120, '3h': 180, '4h': 240, '8h': 480,
        '1d': 1440
    }
    return tf_map.get(interval, 60)

def is_candle_completed(last_candle_time, interval):
    """
    마지막 캔들이 완성되었는지 확인 (TradingView 방식)
    
    ⭐ 시간대 통일 (UTC 기준) - 한국시간/미국시간 문제 해결
    """
    if last_candle_time is None:
        return False
    
    try:
        import pytz
        
        # ⭐ 시간대 정보 추출 및 UTC로 통일
        if hasattr(last_candle_time, 'tzinfo') and last_candle_time.tzinfo is not None:
            # yfinance의 timezone-aware 시간을 UTC로 변환
            last_candle_utc = last_candle_time.astimezone(pytz.UTC)
        else:
            # timezone-naive이면 UTC로 가정
            last_candle_utc = pytz.UTC.localize(last_candle_time)
        
        # 현재 시간을 UTC로 변환 (한국시간 문제 해결)
        now_utc = datetime.now(pytz.UTC)
        
        tf_minutes = get_tf_minutes(interval)
        
        # 마지막 캔들의 종료 시간 (UTC)
        candle_end_time = last_candle_utc + timedelta(minutes=tf_minutes)
        
        # 현재 시간이 캔들 종료 시간보다 뒤에 있으면 완성됨
        is_completed = now_utc >= candle_end_time
        
        logger.debug(f"캔들 상태 (UTC): {last_candle_utc.strftime('%H:%M')} → {candle_end_time.strftime('%H:%M')} (현재: {now_utc.strftime('%H:%M')}) → {'완성 ✅' if is_completed else '미완성 ⏳'}")
        
        return is_completed
    except Exception as e:
        logger.error(f"캔들 완성 판정 에러: {e}")
        return False

def get_ohlc_data(symbol, interval, lookback_days=60):
    """
    OHLC 데이터 수집 (TradingView와 정확히 동일)
    
    ⭐ 중요: 완성된 캔들만 분석
    - 마지막 캔들이 미완성이면 제외
    - 정확한 시간 기반 판정
    """
    if not yf:
        logger.error("❌ yfinance 없음")
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
        
        if df.empty or len(df) < 5:
            logger.debug(f"데이터 부족: {symbol} {interval}")
            return pd.DataFrame()
        
        # ⭐ Pine Script의 barstate.isconfirmed 방식
        # 현재 캔들이 완성됐으면 포함, 미완성이면 제외
        last_candle_time = df.index[-1]
        
        if is_candle_completed(last_candle_time, interval):
            # 캔들이 완성됨 → 마지막 캔들 포함해서 분석 ✅
            logger.debug(f"✅ {symbol} {interval}: 현재 캔들 완성, 포함하여 분석 ({len(df)}개 바)")
            return df
        else:
            # 캔들이 미완성 → 마지막 캔들 제외 ⏳
            logger.debug(f"⏳ {symbol} {interval}: 현재 캔들 미완성, 제외 ({len(df)-1}개 바)")
            df_prev = df.iloc[:-1]
            
            if len(df_prev) < 5:
                logger.debug(f"분석 가능한 캔들 부족: {symbol} {interval}")
                return pd.DataFrame()
            
            return df_prev
    
    except Exception as e:
        logger.error(f"❌ 데이터 수집 실패 ({symbol}, {interval}): {e}")
        return pd.DataFrame()

# ============================================================================
# 신호 스캔 (개선: 캔들 타이밍 + Telegram 포맷)
# ============================================================================
def scan_symbol(detector, symbol, is_korean=False):
    """
    단일 심볼 신호 스캔 (개선판)
    
    ⭐ 중요 변경:
    - 이전 캔들만 분석
    - 신호 상태 추적 (같은 신호 반복 방지)
    - 개선된 Telegram 포맷
    """
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
        asset_type = 'stock'
        base_val = detector._get_base_value(tf_idx, asset_type)
        mult = detector._get_mult_value(tf_idx, asset_type)
        
        # LONG 임계값
        if tf == '4h':
            long_threshold = LONG_4H
        elif tf == '8h':
            long_threshold = LONG_8H
        elif tf == '1d':
            long_threshold = LONG_1D
        else:
            long_threshold = base_val * mult
        
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
            # ⭐ 이전 캔들 시간 추출 (현재 미완성 캔들 이전)
            prev_candle_time = str(ohlc.index[-1])
            current_price = ohlc.iloc[-1]['Close']
            
            # ⭐ 신호 상태 확인 (같은 신호가 반복되지 않도록)
            last_signal_time = get_last_signal_time(symbol, tf, signal_type)
            
            if last_signal_time is None or last_signal_time != prev_candle_time:
                # 새로운 신호
                signals.append((
                    tf, signal_type, total_signals, long_threshold, 
                    short_threshold, mfi_val, current_price, is_korean, prev_candle_time
                ))
                logger.info(f"✅ 신호 감지: {symbol} {tf} {signal_type} (값: {total_signals:.2f})")
            else:
                logger.debug(f"⏭️  중복 신호 무시: {symbol} {tf} {signal_type} (이전과 동일한 캔들)")
    
    return signals

def scan_all():
    """모든 심볼 스캔 (개선: Telegram 포맷 + 캔들 타이밍)"""
    logger.info("=" * 70)
    logger.info(f"🔍 스캔 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"   qual_value: {QUAL_VALUE}, MFI: {USE_MFI}")
    logger.info("=" * 70)
    
    detector = PRZv7ImprovedSignalDetector(qual_value=QUAL_VALUE, use_mfi=USE_MFI)
    total_new_signals = 0
    
    # 미국 주식
    logger.info("📊 미국 S&P 500 상위 300개 스캔 중...")
    us_symbols = get_sp500_symbols()
    
    for idx, symbol in enumerate(us_symbols):
        try:
            signals = scan_symbol(detector, symbol, is_korean=False)
            
            for tf, sig_type, total_sigs, long_th, short_th, mfi_v, current_price, is_kr, prev_time in signals:
                # DB 저장
                log_signal(symbol, tf, sig_type, total_sigs, 
                          long_th if sig_type == 'LONG' else short_th,
                          QUAL_VALUE, mfi_v, prev_time)
                
                # ⭐ 신호 상태 업데이트
                update_signal_status(symbol, tf, sig_type, prev_time)
                
                # ⭐ Telegram 발송 (개선된 포맷)
                send_telegram(symbol, tf, sig_type, current_price, total_sigs, QUAL_VALUE, mfi_v, is_kr)
                
                total_new_signals += 1
            
            if (idx + 1) % 50 == 0:
                logger.info(f"  ✅ {idx + 1}/{len(us_symbols)} 완료...")
        
        except Exception as e:
            logger.error(f"❌ 스캔 실패 ({symbol}): {e}")
            continue
    
    logger.info(f"✅ 미국 스캔 완료: {len(us_symbols)}개 심볼")
    
    # 한국 주식
    logger.info("📊 한국 주식 상위 30개 스캔 중...")
    kr_symbols = get_korean_stocks()
    
    for idx, symbol in enumerate(kr_symbols):
        try:
            signals = scan_symbol(detector, symbol, is_korean=True)
            
            for tf, sig_type, total_sigs, long_th, short_th, mfi_v, current_price, is_kr, prev_time in signals:
                # DB 저장
                log_signal(symbol, tf, sig_type, total_sigs,
                          long_th if sig_type == 'LONG' else short_th,
                          QUAL_VALUE, mfi_v, prev_time)
                
                # ⭐ 신호 상태 업데이트
                update_signal_status(symbol, tf, sig_type, prev_time)
                
                # ⭐ Telegram 발송 (개선된 포맷)
                send_telegram(symbol, tf, sig_type, current_price, total_sigs, QUAL_VALUE, mfi_v, is_kr)
                
                total_new_signals += 1
            
            if (idx + 1) % 10 == 0:
                logger.info(f"  ✅ {idx + 1}/{len(kr_symbols)} 완료...")
        
        except Exception as e:
            logger.error(f"❌ 스캔 실패 ({symbol}): {e}")
            continue
    
    logger.info(f"✅ 한국 스캔 완료: {len(kr_symbols)}개 심볼")
    
    # 완료 로그
    logger.info("=" * 70)
    logger.info(f"✅ 스캔 완료 | 신호: {total_new_signals}개")
    logger.info("=" * 70)
    
    return total_new_signals

# ============================================================================
# 메인 루프
# ============================================================================
def main():
    """메인 루프: 10분마다 반복"""
    logger.info("=" * 70)
    logger.info("🚀 PRZ v7 개선판 (Telegram 포맷 + 캔들 타이밍 수정) 시작")
    logger.info("=" * 70)
    
    init_db()
    
    try:
        iteration = 0
        while True:
            iteration += 1
            logger.info(f"\n⏰ 반복 #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            try:
                scan_all()
            except Exception as e:
                logger.error(f"❌ 스캔 에러: {e}\n{traceback.format_exc()}")
            
            logger.info(f"⏳ {SCAN_INTERVAL}초 대기 중...")
            time.sleep(SCAN_INTERVAL)
    
    except KeyboardInterrupt:
        logger.info("⛔ 키보드 중단")
    except Exception as e:
        logger.error(f"❌ 치명적 에러: {e}\n{traceback.format_exc()}")
    finally:
        logger.info("=" * 70)
        logger.info("⛔ PRZ v7 개선판 종료")
        logger.info("=" * 70)

# ============================================================================
if __name__ == '__main__':
    main()
