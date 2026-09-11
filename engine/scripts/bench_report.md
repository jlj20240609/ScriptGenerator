# M2 案例库跑批报告

- 生成时间：2026-09-11 12:30:09
- 每案例 10 轮；扰动：无；随机种子 11
- 口径（两个都报，不擅自替用户选）：**严格**=文档里拍板的验收定义（ok 且全程没有任何 confirm_request，含脚本自带的「提示我」）；**仅异常**=只把「没找到/做完没看到」算人工介入（脚本自己设计的「提示我」不算）。两者的差值就是「脚本自带的提示我」造成的；误报 = 报成功但终态断言不成立

| 案例 | 轮数 | 成功率 | 无人工介入(严格) | 无人工介入(仅异常) | 误报 | 定位中位 | 校准(基线/异常) | 需要人处理的轮次 |
|---|---|---|---|---|---|---|---|---|
| login_full | 10 | 10/10 (100%) | 0/10 (0%) | 10/10 (100%) | 0 | — ms | 10/0 | [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] |
| login | 10 | 10/10 (100%) | 10/10 (100%) | 10/10 (100%) | 0 | — ms | 10/0 | — |
| erp | 10 | 10/10 (100%) | 10/10 (100%) | 10/10 (100%) | 0 | — ms | 10/0 | — |
| dyn | 10 | 10/10 (100%) | 10/10 (100%) | 10/10 (100%) | 0 | — ms | 10/0 | — |
| feed | 10 | 10/10 (100%) | 10/10 (100%) | 10/10 (100%) | 0 | — ms | 10/0 | — |
| interference | 10 | 10/10 (100%) | 10/10 (100%) | 10/10 (100%) | 0 | — ms | 10/0 | — |

**合计**：60 轮；成功率 60/60（100.0%，目标 ≥95%）；**无人工介入（严格口径，见口径说明）50/60（83.3%，目标 ≥90%）**；无人工介入（只算异常求助）60/60（100.0%）；误报 0（0.0%，目标 ≤2%）
- 最慢轮次：feed #10 3606817ms；feed #9 3572683ms；feed #8 3536670ms

## 未达标轮次明细（每轮都能定位到轮号与原因）

  - [login_full #1] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_001.jpg
  - [login_full #2] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_002.jpg
  - [login_full #3] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_003.jpg
  - [login_full #4] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_004.jpg
  - [login_full #5] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_005.jpg
  - [login_full #6] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_006.jpg
  - [login_full #7] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_007.jpg
  - [login_full #8] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_008.jpg
  - [login_full #9] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_009.jpg
  - [login_full #10] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_010.jpg
