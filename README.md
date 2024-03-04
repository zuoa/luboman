

docker run -P --name luboman -v /data/luboman:/data -v ~/.bypy:/root/.bypy -p 5001:5001 -d --restart always ghcr.io/zuoa/luboman:main
