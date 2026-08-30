sudo -v

sudo cpupower frequency-set -g performance

nvidia-settings -a '[gpu:0]/GpuPowerMizerMode=1'


echo off | sudo tee /sys/devices/system/cpu/smt/control

sudo sysctl vm.swappiness=10

sudo modprobe ntsync

