import os
from contextlib import contextmanager

import akshare as ak
import pandas as pd

from Common.CEnum import AUTYPE, DATA_FIELD, KL_TYPE
from Common.CTime import CTime
from Common.func_util import str2float
from KLine.KLine_Unit import CKLine_Unit

from .CommonStockAPI import CCommonStockApi


def parse_code(code):
    """解析证券代码，返回 (交易所前缀, 6位数字)。

    既支持带前缀(sz.000001 / sh510310 / sz000001)，也支持纯数字代码。
    纯数字时按代码段自动推断交易所：
      6/5/9 开头 -> sh(上交所)   0/1/2/3 开头 -> sz(深交所)   4/8 开头 -> bj(北交所)
    注意：纯 000001 默认按深市平安银行处理；上证指数请显式写 sh.000001。
    """
    s = code.replace(".", "").lower().strip()
    if s[:2] in ("sh", "sz", "bj"):
        return s[:2], s[2:]
    if s.startswith(("5", "6", "9")):
        prefix = "sh"
    elif s.startswith(("0", "1", "2", "3")):
        prefix = "sz"
    elif s.startswith(("4", "8")):
        prefix = "bj"
    else:
        prefix = "sh"
    return prefix, s


def detect_market(code):
    """根据代码格式自动识别市场，返回 'A' / 'HK' / 'US'。

    规则（与 parse_code 的纯数字推断不冲突——A股一律6位，港股5位）：
      - A股/ETF/指数：纯6位数字，或 sh/sz/bj 前缀
      - 港股：纯5位数字（如 00700），或 hk 前缀（hk00700）
      - 美股：含字母或 '.'（如 AAPL、105.AAPL、us.AAPL）
    """
    s = str(code).strip().lower()
    if s.startswith("hk"):
        return "HK"
    if s.startswith("us") and not s[2:].isdigit():
        # us.AAPL / usAAPL（usXXXX 纯数字交给后面按数字判断）
        return "US"
    if s.replace(".", "").isalnum() and not s.replace(".", "").isdigit():
        # 含字母 -> 美股 ticker 或 105.AAPL
        return "US"
    digits = s
    if digits[:2] in ("sh", "sz", "bj"):
        return "A"
    if digits.isdigit():
        if len(digits) == 5:
            return "HK"
        return "A"
    return "A"


def parse_time(date_val):
    """解析为 CTime，支持 Timestamp / 字符串（YYYY-MM-DD[ HH:MM[:SS]] 或 YYYYMMDD），保留时分"""
    if isinstance(date_val, pd.Timestamp):
        return CTime(date_val.year, date_val.month, date_val.day, date_val.hour, date_val.minute)
    s = str(date_val)
    date_str = s[:10]
    if "-" in date_str:  # 2021-09-13 或 2021-09-13 14:30:00
        year, month, day = int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10])
    else:  # 20210913
        year, month, day = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
    hour = minute = 0
    if len(s) >= 16 and ":" in s[10:]:  # 含 HH:MM
        hour, minute = int(s[11:13]), int(s[14:16])
    return CTime(year, month, day, hour, minute)


def fetch_sina_name(sina_symbol):
    """从新浪实时行情接口获取证券名称；失败返回空串（不影响主流程）"""
    try:
        import requests
        url = f"https://hq.sinajs.cn/list={sina_symbol}"
        r = requests.get(url, headers={"Referer": "https://finance.sina.com.cn"},
                         timeout=5, proxies={"http": None, "https": None})
        if '="' in r.text:
            return r.text.split('="')[1].split(',')[0].strip()
    except Exception:
        pass
    return ""


# 港股/美股名称缓存（新浪实时行情，避免重复请求）
_US_NAME_CACHE = {}
_HK_NAME_CACHE = {}


@contextmanager
def _no_proxy():
    """临时绕过系统/环境代理直连（东财在系统代理下连不通时使用）。

    清空 *_proxy 环境变量并设 NO_PROXY=* —— requests 据此对所有 host
    跳过 trust_env 推断出的系统代理；退出时原样恢复，不污染全局。
    """
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"]
    original = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        yield
    finally:
        for k, v in original.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _hk_symbol(code):
    """港股代码归一为5位数字（去掉 hk 前缀、补零）"""
    s = str(code).strip().lower()
    if s.startswith("hk"):
        s = s[2:]
    return s.zfill(5)


def _us_symbol(code):
    """美股代码归一为新浪 ticker（去掉 us. 前缀，大写）。如 us.AAPL -> AAPL"""
    s = str(code).strip()
    if s.lower().startswith("us."):
        s = s[3:]
    elif s.lower().startswith("us") and not s[2:3].isdigit():
        s = s[2:]
    if "." in s and s.split(".", 1)[0].isdigit():
        s = s.split(".", 1)[1]  # 容错：105.AAPL -> AAPL
    return s.upper()


def _fetch_hk_name(code):
    """港股名称（新浪实时行情 rt_hkXXXXX，取中文名字段，best-effort）"""
    symbol = _hk_symbol(code)
    if symbol not in _HK_NAME_CACHE:
        # rt_hk 行情格式: "英文名,中文名,今开,昨收,..."，取第2个字段（中文名）
        name = ""
        try:
            import requests
            r = requests.get(f"https://hq.sinajs.cn/list=rt_hk{symbol}",
                             headers={"Referer": "https://finance.sina.com.cn"},
                             timeout=5, proxies={"http": None, "https": None})
            if '="' in r.text:
                fields = r.text.split('="')[1].split('";')[0].split(",")
                if len(fields) >= 2 and fields[1].strip():
                    name = fields[1].strip()       # 中文名
                elif fields and fields[0].strip():
                    name = fields[0].strip()       # 退而取英文名
        except Exception:
            pass
        _HK_NAME_CACHE[symbol] = name
    return _HK_NAME_CACHE[symbol]


def _fetch_us_name(code):
    """美股名称（新浪实时行情 gb_<ticker>，best-effort，取不到返回空串）"""
    ticker = _us_symbol(code)
    if ticker not in _US_NAME_CACHE:
        _US_NAME_CACHE[ticker] = fetch_sina_name(f"gb_{ticker.lower()}")
    return _US_NAME_CACHE[ticker]


def get_security_name(code):
    """按市场获取证券名称，供界面层统一调用；失败返回空串。"""
    market = detect_market(code)
    if market == "HK":
        return _fetch_hk_name(code)
    if market == "US":
        return _fetch_us_name(code)
    prefix, num = parse_code(code)
    return fetch_sina_name(prefix + num)


def create_item_dict(row):
    """将（英文列名的）DataFrame行转换为K线单元所需的字典格式"""
    item = {
        DATA_FIELD.FIELD_TIME: parse_time(row['date']),
        DATA_FIELD.FIELD_OPEN: str2float(row['open']),
        DATA_FIELD.FIELD_HIGH: str2float(row['high']),
        DATA_FIELD.FIELD_LOW: str2float(row['low']),
        DATA_FIELD.FIELD_CLOSE: str2float(row['close']),
    }
    if 'volume' in row:
        item[DATA_FIELD.FIELD_VOLUME] = str2float(row['volume'])
    if 'amount' in row:
        item[DATA_FIELD.FIELD_TURNOVER] = str2float(row['amount'])
    if 'turnover' in row:
        item[DATA_FIELD.FIELD_TURNRATE] = str2float(row['turnover'])
    return item


class CAkshare(CCommonStockApi):
    """使用 akshare 获取A股数据（新浪源，无需代理，走443端口）"""

    def __init__(self, code, k_type=KL_TYPE.K_DAY, begin_date=None, end_date=None, autype=AUTYPE.QFQ):
        super(CAkshare, self).__init__(code, k_type, begin_date, end_date, autype)

    def __sina_symbol(self):
        # 统一为新浪格式: sz000001 / sh510310
        return self._prefix + self._num

    def get_kl_data(self):
        """获取K线数据。A股走新浪源；港股/美股走东财源。"""
        if self.market in ("HK", "US"):
            yield from self.__get_em_kl_data()
            return

        adjust_dict = {AUTYPE.QFQ: "qfq", AUTYPE.HFQ: "hfq", AUTYPE.NONE: ""}
        adjust = adjust_dict.get(self.autype, "qfq")

        start_date = self.begin_date.replace("-", "") if self.begin_date else "19900101"
        end_date = self.end_date.replace("-", "") if self.end_date else "20991231"

        symbol = self.__sina_symbol()
        minute_period = self.__minute_period()
        if minute_period is not None:
            # 分钟级：新浪分钟线（仅个股，且只返回最近一段数据，无法指定起止）
            if not self.is_stock:
                raise Exception("akshare(新浪源)分钟数据仅支持个股，指数/ETF不支持")
            df = ak.stock_zh_a_minute(symbol=symbol, period=minute_period, adjust=adjust)
            df = df.rename(columns={'day': 'date'})  # 新浪分钟时间列名为 day
        elif self.is_etf:
            # ETF：新浪日线（不支持复权）
            df = ak.fund_etf_hist_sina(symbol=symbol)
        elif self.is_stock:
            # 个股：新浪日线，自带复权
            df = ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date, adjust=adjust)
        else:
            # 指数：新浪指数日线（不支持复权）
            df = ak.stock_zh_index_daily(symbol=symbol)

        # 统一日期为 datetime 以便筛选/重采样
        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        beg = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date) + pd.Timedelta(days=1)  # 含当天分钟线
        df = df[(df['date'] >= beg) & (df['date'] < end)]

        # 日线以上做重采样（分钟级直接返回新浪原始周期）
        if minute_period is None:
            df = self.__resample(df)

        for _, row in df.iterrows():
            yield CKLine_Unit(create_item_dict(row))

    def __get_em_kl_data(self):
        """港股/美股K线（新浪源，仅日线；周/月线靠重采样，不支持分钟级）"""
        if self.__minute_period() is not None:
            raise Exception("港股/美股暂不支持分钟级K线（仅A股个股支持）")
        if self.k_type not in (KL_TYPE.K_DAY, KL_TYPE.K_WEEK, KL_TYPE.K_MON):
            raise Exception(f"港股/美股不支持{self.k_type}级别的K线数据")

        adjust_dict = {AUTYPE.QFQ: "qfq", AUTYPE.HFQ: "hfq", AUTYPE.NONE: ""}
        adjust = adjust_dict.get(self.autype, "qfq")
        start_date = self.begin_date.replace("-", "") if self.begin_date else "19900101"
        end_date = self.end_date.replace("-", "") if self.end_date else "20991231"

        # 新浪源：港股 stock_hk_daily / 美股 stock_us_daily，返回英文列日线
        with _no_proxy():
            if self.market == "HK":
                df = ak.stock_hk_daily(symbol=_hk_symbol(self.code), adjust=adjust)
            else:  # US（新浪用裸 ticker，不复权——美股新浪仅原始价）
                df = ak.stock_us_daily(symbol=_us_symbol(self.code), adjust=adjust)

        if df is None or df.empty:
            raise Exception(f"未获取到 {self.code} 的行情数据（检查代码/日期/网络）")

        df = df.copy()
        df['date'] = pd.to_datetime(df['date'])
        beg = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date) + pd.Timedelta(days=1)
        df = df[(df['date'] >= beg) & (df['date'] < end)]

        # 新浪只给日线，周/月线复用 A股的重采样逻辑
        df = self.__resample(df)

        for _, row in df.iterrows():
            yield CKLine_Unit(create_item_dict(row))

    def __minute_period(self):
        """若 k_type 是分钟级，返回新浪所需的分钟字符串，否则返回 None"""
        _dict = {
            KL_TYPE.K_1M: '1',
            KL_TYPE.K_5M: '5',
            KL_TYPE.K_15M: '15',
            KL_TYPE.K_30M: '30',
            KL_TYPE.K_60M: '60',
        }
        return _dict.get(self.k_type)

    def __resample(self, df):
        """将日线重采样为周/月线；日线直接返回"""
        if self.k_type == KL_TYPE.K_DAY:
            return df
        rule_dict = {KL_TYPE.K_WEEK: 'W', KL_TYPE.K_MON: 'MS'}
        if self.k_type not in rule_dict:
            raise Exception(f"akshare(新浪源)不支持{self.k_type}级别的K线数据")
        agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}
        for col in ('volume', 'amount', 'turnover'):
            if col in df.columns:
                agg[col] = 'sum'
        out = df.set_index('date').resample(rule_dict[self.k_type]).agg(agg).dropna(subset=['open'])
        return out.reset_index()

    def SetBasciInfo(self):
        """设置基本信息：识别市场，判断股票/指数/ETF，取名称"""
        self.name = self.code
        self.market = detect_market(self.code)

        if self.market == "HK":
            self.is_etf = False
            self.is_stock = True
            real_name = _fetch_hk_name(self.code)
            if real_name:
                self.name = real_name
            return
        if self.market == "US":
            self.is_etf = False
            self.is_stock = True
            real_name = _fetch_us_name(self.code)
            if real_name:
                self.name = real_name
            return

        self._prefix, self._num = parse_code(self.code)
        num = self._num
        # ETF：上交所 5xxxxx / 深交所 1xxxxx
        self.is_etf = num.startswith('5') or num.startswith('1')
        # 指数：上证 sh000xxx / 深证 sz399xxx
        is_index = (self._prefix == 'sh' and num.startswith('000')) or \
                   (self._prefix == 'sz' and num.startswith('399'))
        self.is_stock = not is_index and not self.is_etf

        # 取真实证券名称（取不到则保留代码，不影响绘图）
        real_name = fetch_sina_name(self._prefix + self._num)
        if real_name:
            self.name = real_name

    @classmethod
    def do_init(cls):
        """初始化 (akshare不需要登录)"""
        pass

    @classmethod
    def do_close(cls):
        """关闭 (akshare不需要登出)"""
        pass
