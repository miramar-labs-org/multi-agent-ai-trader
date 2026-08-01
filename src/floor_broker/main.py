import uvicorn


def main():
    uvicorn.run("src.floor_broker.app:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
