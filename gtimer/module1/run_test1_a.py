import gtimer as gt
import globalholder as g
import test1_b
import time

print g.tif

time.sleep(0.1)
gt.stamp('first')
time.sleep(0.2)
gt.stamp('second')
test1_b.func()
time.sleep(0.1)
gt.stamp('third')


print g.tif
print g.tif.times.stamps
