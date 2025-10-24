# 小牛API接口测试
就咋说呢，小牛也是牛
小牛在web端使用用户名+密码方式登录，没有MFA，没有随机挑战，没有反机器人验证。
而且在web端和APP端的token是通用的
所以我们可以在web端登录后拿到token，用这个token可以完成所有车机控制接口的鉴权。

# 登录方式
https://account.niu.com/v3/login
使用用户名+密码方式登录，F12记录提交表单中的
account
password
并修改config.ini中的对应字段。
app_id字段看起来是跟随小牛的发版更新的，目前记录到的两个可用的
niu_h8nv8eaz
niu_n0dzndn6

# 关于如何搭建Traccar
转头去问GPT就行了

# 关于Traccar Osmand协议上报
脚本会自动获取名下车辆SN列表，并以SN为上报标识，Traccar上添加的标识符为SN
