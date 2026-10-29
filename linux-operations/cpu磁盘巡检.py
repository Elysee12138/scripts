import datetime
import psutil


now = datetime.datetime.now()

print("系统巡检时间：", now)
print("--------------------")

# 这一部分是CPU
cpu = psutil.cpu_percent(interval=1)
print("CPU使用率：", cpu, "%")

if cpu > 80:
    print("警告：CPU使用率较高")
else:
    print("CPU使用正常")

# 这一部分是内存
memory = psutil.virtual_memory()
memory_percent = memory.percent
print("内存使用率：", memory_percent, "%")

if memory_percent > 80:
    print("警告：内存使用率较高")
else:
    print("内存使用正常")

# 这一部分是磁盘
disk = psutil.disk_usage("/")
disk_percent = disk.percent
print("根目录磁盘使用率：", disk_percent, "%")

if disk_percent > 80:
    print("警告：磁盘空间不足")
else:
    print("磁盘使用正常")

print("--------------------")
print("系统巡检完成")
