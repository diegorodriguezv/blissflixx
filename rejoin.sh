#!/bin/bash
check_interval=10
ping_destination=192.168.1.13
ping_count=3
max_ping_failures=3
failure_count=0
while true
do
    ping -c $ping_count $ping_destination > /dev/null 2>&1
    if [[ $? -eq 0 ]]; then
        failure_count=0
        echo 'All good as of:' $(date)
    else
        failure_count=$((failure_count+1))
        echo $failure_count 'Network failure at:' $(date)
        if ((failure_count >= max_ping_failures)); then
            echo 'Recovering network stack at:' $(date)
            sudo ifconfig wlan0 down
            sleep 5
            sudo ifconfig wlan0 up
            sleep 5
            sudo ifconfig wlan0 up
            sleep 5
            sudo ifconfig wlan0 up
            failure_count=0
	    echo 'recovered at:' $(date)
        fi
    fi
    sleep $check_interval
done
