# AI 주식/코인 자동매매 프로젝트
> 최종 업데이트: 2026-05-22 00:45

## 인프라
- Mac Mini arm64, macOS Darwin 25.4.0
- Python 3.12.13 (Freqtrade), Go 1.26.3 (tossctl)
- Freqtrade 2026.4 + CCXT 4.5.54
- venv: `~/trading/ft_env`
- 작업 디렉토리: `~/trading/`

## Freqtrade (코인 모의투자)
- 설정: `~/trading/freqtrade_userdata/config_upbit_dryrun.json`
- 거래소: 업비트 (dry-run)
- 가상 잔고: 1,000,000 KRW / 건당 50,000 / 최대 3건
- 종목: BTC, ETH, XRP, SOL, ADA (KRW 마켓)
- launchd: `~/Library/LaunchAgents/com.curara.freqtrade-dryrun.plist`
- API: http://127.0.0.1:8080 (freqtrade:freqtrade)

## 전략 (`~/trading/freqtrade_userdata/strategies/`)
| 파일 | 레벨 | 설명 | 상태 |
|------|------|------|------|
| SimpleStrategy.py | L1 | SMA20/50 + RSI, 5분봉 | 대기 |
| AdvancedStrategy.py | L2 | BB+MACD+RSI+Vol+ATR동적손절+트레일링, 15분봉 | 대기 |
| AdvancedStrategyV2.py | L2+ | V2 3시그널+Hyperopt최적화, 15분봉 | 대기 |
| LLMSentimentStrategy.py | L3 | L2 + Tavily뉴스 + Claude Haiku 감성분석 | **가동중** |

## Hyperopt 최적화 결과 (2026-05-21)
- 500 epochs, SharpeHyperOptLossDaily, 87일 데이터
- 최적 파라미터: `AdvancedStrategyV2.json`
  - buy_rsi=50, sell_rsi=64
  - trailing_stop_positive=0.019, offset=0.031
  - stoploss=-0.08, atr_multiplier=3.1
- 백테스트: 68거래, 승률60.3%, +3,106KRW(+0.31%), Sharpe 1.89
- 이전(default): -0.26% → 최적화후: **+0.31%** (손실→흑자 전환)

## 토스증권 CLI (주식)
- 바이너리: `~/trading/tossctl`
- 소스: `~/trading/tossinvest-cli/`
- 인증: 완료 (만료 2027-05-21)
- 설정: `~/Library/Application Support/tossctl/config.json`
- 거래기능: 전부 OFF (조회만)
- 보유: 하림(A136480) 1주, 한화손해보험(A000370) 1주

## NotebookLM
- 노트북: "ai 활용 주식투자"
- ID: `f13e279b-5d47-48e7-9fd1-99c1d2b47a9e`
- 소스: 35개 (GitHub, YouTube, 강의, 가이드)
- conversation_id: `8a8a0479-5c2d-4cf9-9914-7205e4734cb0`

## 관리 명령어
```bash
# Freqtrade 상태
launchctl list | grep freqtrade
tail -f ~/trading/freqtrade_userdata/logs/launchd_stderr.log
curl -s http://127.0.0.1:8080/api/v1/status -u freqtrade:freqtrade

# Freqtrade 재시작
launchctl unload ~/Library/LaunchAgents/com.curara.freqtrade-dryrun.plist
launchctl load ~/Library/LaunchAgents/com.curara.freqtrade-dryrun.plist

# 토스증권
~/trading/tossctl account summary
~/trading/tossctl portfolio positions
~/trading/tossctl quote AAPL

# 전략 변경 (plist에서 전략명 수정 후 재시작)
```

## 다음 단계
- [x] 백테스팅 (AdvancedStrategy 과거데이터 검증) ✓
- [x] Hyperopt 파라미터 최적화 ✓ (V2, 승률60%, +0.31%)
- [x] V2 전략 라이브 전환 ✓ (launchd 봇 AdvancedStrategyV2로 교체)
- [x] 텔레그램 봇 연동 ✓ (@curara_trading_bot, 실시간 알림)
- [x] LLM전략 API키 ✓ (Tavily 무료 + Anthropic $4.33 크레딧)
- [x] L3 LLMSentimentStrategy 가동 ✓ (4시간 캐시, 무료 범위)
- [x] 자동 재최적화 스케줄 ✓ (매주 일요일 04:00 Hyperopt)
- [ ] 실전전환 (소액, 업비트 API키 발급)
- [ ] V2/LLM 모의투자 모니터링 후 결과 평가
- [ ] 한국투자증권 KIS Developers 가입 + 모의투자 API키 발급
- [ ] 주식 자동매매 봇 구축 (KIS API 기반)

## 한국투자증권 (주식)
- 계좌개설: 완료 (2026-05-22)
- 위탁계좌: 44594535-01
- CMA: 44594535-21
- ISA 중개형: 44594536-01
- KIS Developers: 미가입 (다음 스텝)
- 모의투자 API: 미발급
