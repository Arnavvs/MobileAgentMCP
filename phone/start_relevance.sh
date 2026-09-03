# Start the on-device relevance server in Termux.
#   .\tmx.ps1 -File .\phone\start_relevance.sh
# then, from the PC:
#   adb -s <serial> forward tcp:8765 tcp:8765
H=/data/data/com.termux/files/home/relevance
mkdir -p $H
cd $H

# The model is ~30 MB and the phone fetches it far faster than adb can push it.
if [ ! -s potion-base-8M/model.safetensors ]; then
  echo "fetching potion-base-8M ..."
  mkdir -p potion-base-8M
  B=https://huggingface.co/minishlab/potion-base-8M/resolve/main
  for f in model.safetensors vocab.txt config.json; do
    curl -sL -o "potion-base-8M/$f" "$B/$f"
  done
fi

# numpy is the only dependency, and Termux ships a prebuilt aarch64 package -
# pip would compile it from source.
python -c "import numpy" 2>/dev/null || {
  echo "installing numpy ..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y python-numpy 2>&1 | tail -2
}

pkill -f relevance_server.py 2>/dev/null
sleep 1
nohup python relevance_server.py > server.log 2>&1 &
sleep 4
cat server.log
curl -s http://127.0.0.1:8765/health
echo
