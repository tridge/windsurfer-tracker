ssh fjall "tshark -i eno1 -w - -f 'port 41234'" | wireshark -k -i -
