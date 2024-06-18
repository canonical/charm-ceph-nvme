#!/bin/bash
charm=$(grep "name" metadata.yaml | awk '{print $2}')
echo "renaming ${charm}_*.charm to ${charm}.charm"
echo -n "pwd: "
pwd
ls -al
echo "Removing bad downloaded charm maybe?"
if [[ -e "${charm}.charm" ]];
then
    rm "${charm}.charm"
fi
echo "Renaming charm here."
mv ${charm}_*.charm ${charm}.charm
