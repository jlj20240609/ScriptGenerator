# M2 案例库跑批报告

- 生成时间：2026-09-11 13:37:23
- 每案例 50 轮；扰动：无；随机种子 11
- 口径（两个都报，不擅自替用户选）：**严格**=文档里拍板的验收定义（ok 且全程没有任何 confirm_request，含脚本自带的「提示我」）；**仅异常**=只把「没找到/做完没看到」算人工介入（脚本自己设计的「提示我」不算）。两者的差值就是「脚本自带的提示我」造成的；误报 = 报成功但终态断言不成立

| 案例 | 轮数 | 成功率 | 无人工介入(严格) | 无人工介入(仅异常) | 误报 | 定位中位 | 校准(基线/异常) | 需要人处理的轮次 |
|---|---|---|---|---|---|---|---|---|
| login_full | 50 | 49/50 (98%) | 0/50 (0%) | 49/50 (98%) | 0 | — ms | 50/1 | [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50] |
| login | 50 | 50/50 (100%) | 50/50 (100%) | 50/50 (100%) | 0 | — ms | 50/0 | — |
| erp | 50 | 50/50 (100%) | 50/50 (100%) | 50/50 (100%) | 0 | — ms | 50/0 | — |
| dyn | 50 | 50/50 (100%) | 50/50 (100%) | 50/50 (100%) | 0 | — ms | 50/0 | — |
| feed | 50 | 50/50 (100%) | 50/50 (100%) | 50/50 (100%) | 0 | — ms | 50/2 | — |
| interference | 50 | 50/50 (100%) | 50/50 (100%) | 50/50 (100%) | 0 | — ms | 50/0 | — |

**合计**：300 轮；成功率 299/300（99.7%，目标 ≥95%）；**无人工介入（严格口径，见口径说明）250/300（83.3%，目标 ≥90%）**；无人工介入（只算异常求助）299/300（99.7%）；误报 0（0.0%，目标 ≤2%）
- 最慢轮次：login_full #22 130048ms；login_full #21 20726ms；login_full #1 19813ms

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
  - [login_full #11] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_011.jpg
  - [login_full #12] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_012.jpg
  - [login_full #13] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_013.jpg
  - [login_full #14] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_014.jpg
  - [login_full #15] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_015.jpg
  - [login_full #16] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_016.jpg
  - [login_full #17] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_017.jpg
  - [login_full #18] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_018.jpg
  - [login_full #19] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_019.jpg
  - [login_full #20] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_020.jpg
  - [login_full #21] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_021.jpg
  - [login_full #22] status=failed 需人处理=12['not_found', 'not_found', 'not_found', 'not_found', 'not_found', 'not_found', 'not_found', 'not_found', 'not_found', 'not_found', 'not_found', 'not_found'] 断言=None calib=['first_run', 'page_not_found'] move=None err=轮次异常：error(1400, 'GetWindowRect', '无效的窗口句柄。')
  - [login_full #23] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_023.jpg
  - [login_full #24] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_024.jpg
  - [login_full #25] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_025.jpg
  - [login_full #26] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_026.jpg
  - [login_full #27] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_027.jpg
  - [login_full #28] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_028.jpg
  - [login_full #29] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_029.jpg
  - [login_full #30] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_030.jpg
  - [login_full #31] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_031.jpg
  - [login_full #32] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_032.jpg
  - [login_full #33] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_033.jpg
  - [login_full #34] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_034.jpg
  - [login_full #35] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_035.jpg
  - [login_full #36] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_036.jpg
  - [login_full #37] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_037.jpg
  - [login_full #38] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_038.jpg
  - [login_full #39] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_039.jpg
  - [login_full #40] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_040.jpg
  - [login_full #41] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_041.jpg
  - [login_full #42] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_042.jpg
  - [login_full #43] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_043.jpg
  - [login_full #44] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_044.jpg
  - [login_full #45] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_045.jpg
  - [login_full #46] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_046.jpg
  - [login_full #47] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_047.jpg
  - [login_full #48] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_048.jpg
  - [login_full #49] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_049.jpg
  - [login_full #50] status=ok 需人处理=0['notify'] 断言=True calib=['first_run'] move=None err=None shot=engine/scripts/bench_shots/login_full_050.jpg
