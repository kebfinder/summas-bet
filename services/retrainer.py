from engine.self_learning_model import train

def retrain():
    X = [[1.5, 1.2], [2.0, 1.0], [1.0, 2.0]]
    y = [1, 1, 0]

    train(X, y)