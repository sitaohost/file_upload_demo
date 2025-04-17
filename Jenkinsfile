pipeline {
    agent any
    
    environment {
        DOCKER_IMAGE = "myapp:latest"
    }
    
    stages {
        stage('Checkout') {
            steps {
                git branch: 'main', 
                url: 'https://github.com/sitaohost/file_upload_demo.git' 
            }
        }
        
        stage('构建Docker镜像') {
            steps {
                sh 'docker build -t ${DOCKER_IMAGE} .'
            }
        }
        
        stage('部署') {
            steps {
                sh '''
                docker stop myapp || true
                docker rm myapp || true
                docker run -d -p 8000:8000 --name myapp ${DOCKER_IMAGE}
                '''
            }
        }
    }
}