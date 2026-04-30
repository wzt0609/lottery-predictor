# 彩票数据预测系统

基于统计分析的彩票数据预测工具，支持福彩3D、排列三、排列五。

## 运行方式

```bash
python lottery_predictor.py predict   # 生成预测
python lottery_predictor.py post-draw # 开奖后校验
```

## GitHub Actions 自动运行

- 每天 **20:00 CST** (12:00 UTC) → 自动预测
- 每天 **21:45 CST** (13:45 UTC) → 自动校验
- 结果自动部署到 GitHub Pages

## 手机查看

部署完成后，访问 `https://你的用户名.github.io/lottery-predictor/`

⚠️ 彩票具有随机性，以上仅供娱乐参考，请理性购彩。
