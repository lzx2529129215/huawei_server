// Continuous video-only evidence capture. No microphone, audio, policy or score changes.
#include <multimedia/player_framework/native_avscreen_capture.h>
#include <multimedia/player_framework/native_avbuffer.h>
#include <native_buffer/native_buffer.h>
#include <native_buffer/buffer_common.h>
#include <zlib.h>
#include <array>
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <mutex>
#include <string>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <vector>

namespace {
constexpr int width=1560, height=1040;
constexpr int64_t second=1000000000;
std::atomic<bool> stopped{false};
int64_t ticks(clockid_t id) {
    timespec ts{};
    if(clock_gettime(id,&ts)!=0) return -1;
    return static_cast<int64_t>(ts.tv_sec)*second+ts.tv_nsec;
}
void be32(unsigned char* dst,uint32_t value) {
    for(unsigned i=0;i<4;++i) dst[i]=static_cast<unsigned char>(value>>(24-8*i));
}
bool chunk(FILE* file,const char* type,const unsigned char* data,size_t length) {
    unsigned char bytes[4]; be32(bytes,static_cast<uint32_t>(length));
    uLong crc=crc32(0,reinterpret_cast<const Bytef*>(type),4);
    if(length) crc=crc32(crc,data,static_cast<uInt>(length));
    bool ok=std::fwrite(bytes,1,4,file)==4 && std::fwrite(type,1,4,file)==4;
    if(length) ok=ok && std::fwrite(data,1,length,file)==length;
    be32(bytes,static_cast<uint32_t>(crc));
    return ok && std::fwrite(bytes,1,4,file)==4;
}
struct Capture {
    std::string directory;
    FILE* log=nullptr;
    std::mutex mutex;
    std::vector<unsigned char> filtered=std::vector<unsigned char>((width*4+1)*height);
    std::vector<unsigned char> compressed=std::vector<unsigned char>(compressBound((width*4+1)*height));
    z_stream deflater{};
    int64_t previous=0;
    unsigned frames=0;
    unsigned errors=0;
    void event(const char* name,int value) {
        std::lock_guard<std::mutex> lock(mutex);
        std::fprintf(log,"event\t%s\t%d\t%lld\n",name,value,static_cast<long long>(ticks(CLOCK_BOOTTIME)));
        std::fflush(log);
    }
};
void onState(OH_AVScreenCapture*,OH_AVScreenCaptureStateCode state,void* data) {
    static_cast<Capture*>(data)->event("state",state);
}
void onError(OH_AVScreenCapture*,int32_t error,void* data) {
    static_cast<Capture*>(data)->event("error",error);
}
void onData(OH_AVScreenCapture*,OH_AVBuffer* buffer,OH_AVScreenCaptureBufferType type,
            int64_t timestamp,void* data) {
    if(type!=OH_SCREEN_CAPTURE_BUFFERTYPE_VIDEO || stopped.load()) return;
    auto& state=*static_cast<Capture*>(data);
    std::lock_guard<std::mutex> lock(state.mutex);
    const int64_t begin=ticks(CLOCK_BOOTTIME);
    if(begin-state.previous<second) return;
    state.previous=begin;
    const int64_t mono=ticks(CLOCK_MONOTONIC);
    auto* native=OH_AVBuffer_GetNativeBuffer(buffer);
    OH_NativeBuffer_Config config{};
    if(native) OH_NativeBuffer_GetConfig(native,&config);
    void* pixels=nullptr;
    int error=0;
    if(!native || config.width!=width || config.height!=height || config.stride<width*4 ||
       (config.format!=NATIVEBUFFER_PIXEL_FMT_RGBA_8888 && config.format!=NATIVEBUFFER_PIXEL_FMT_BGRA_8888)) error=1;
    if(!error && OH_NativeBuffer_Map(native,&pixels)!=0) error=2;
    if(!error && !pixels) error=3;
    if(!error) {
        const bool bgra=config.format==NATIVEBUFFER_PIXEL_FMT_BGRA_8888;
        for(int y=0;y<height;++y) {
            auto* dst=state.filtered.data()+static_cast<size_t>(y)*(width*4+1);
            const auto* src=static_cast<unsigned char*>(pixels)+static_cast<size_t>(y)*static_cast<size_t>(config.stride);
            dst[0]=0;
            for(int x=0;x<width;++x) {
                dst[1+x*4]=src[x*4+(bgra?2:0)]; dst[2+x*4]=src[x*4+1];
                dst[3+x*4]=src[x*4+(bgra?0:2)]; dst[4+x*4]=255;
            }
        }
    }
    if(pixels && OH_NativeBuffer_Unmap(native)!=0) error=4;
    if(native) OH_NativeBuffer_Unreference(native);
    size_t bytes=0;
    char name[64]; std::snprintf(name,sizeof(name),"frame_%06u.png",state.frames);
    if(!error) {
        deflateReset(&state.deflater);
        state.deflater.next_in=state.filtered.data();
        state.deflater.avail_in=static_cast<uInt>(state.filtered.size());
        state.deflater.next_out=state.compressed.data();
        state.deflater.avail_out=static_cast<uInt>(state.compressed.size());
        if(deflate(&state.deflater,Z_FINISH)!=Z_STREAM_END) error=5;
        bytes=state.deflater.total_out;
    }
    if(!error) {
        const auto path=state.directory+"/"+name;
        const int fd=open(path.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);
        FILE* file=fd<0?nullptr:fdopen(fd,"wb");
        if(!file) { if(fd>=0) close(fd); error=6; }
        else {
            const unsigned char signature[]={137,80,78,71,13,10,26,10};
            std::array<unsigned char,13> ihdr{};
            be32(ihdr.data(),width); be32(ihdr.data()+4,height); ihdr[8]=8; ihdr[9]=6;
            bool ok=std::fwrite(signature,1,8,file)==8 && chunk(file,"IHDR",ihdr.data(),ihdr.size()) &&
                    chunk(file,"IDAT",state.compressed.data(),bytes) && chunk(file,"IEND",nullptr,0);
            if(std::fclose(file)!=0) ok=false;
            if(!ok) error=7;
        }
    }
    const int64_t end=ticks(CLOCK_BOOTTIME);
    std::fprintf(state.log,"frame\t%u\t%lld\t%lld\t%lld\t%lld\t%d\t%d\t%d\t%d\t%zu\t%d\t%s\n",
        state.frames,static_cast<long long>(begin),static_cast<long long>(end),
        static_cast<long long>(timestamp),static_cast<long long>(mono),config.width,config.height,
        config.stride,config.format,bytes,error,name);
    std::fflush(state.log);
    ++state.frames;
    if(error) ++state.errors;
}
}
int main(int argc,char** argv) {
    constexpr const char* prefix="/data/local/tmp/memscenes_screen_stream_";
    if(argc!=3 || std::strncmp(argv[1],prefix,std::strlen(prefix))!=0 || std::strstr(argv[1],"..")) return 2;
    char* tail=nullptr;
    const long duration=std::strtol(argv[2],&tail,10);
    if(!tail || *tail || duration<1 || duration>900) return 2;
    if(mkdir(argv[1],0700)!=0) return 3;
    Capture state; state.directory=argv[1];
    state.log=std::fopen((state.directory+"/frames.tsv").c_str(),"wx");
    if(!state.log || deflateInit(&state.deflater,Z_BEST_SPEED)!=Z_OK) return 4;
    auto* capture=OH_AVScreenCapture_Create();
    if(!capture) return 5;
    state.event("pid",getpid());
    OH_AVScreenCaptureConfig config{};
    config.captureMode=OH_CAPTURE_HOME_SCREEN; config.dataType=OH_ORIGINAL_STREAM;
    config.videoInfo.videoCapInfo.videoFrameWidth=width;
    config.videoInfo.videoCapInfo.videoFrameHeight=height;
    config.videoInfo.videoCapInfo.videoSource=OH_VIDEO_SOURCE_SURFACE_RGBA;
    state.event("microphone_disabled",OH_AVScreenCapture_SetMicrophoneEnabled(capture,false));
    state.event("state_callback",OH_AVScreenCapture_SetStateCallback(capture,onState,&state));
    state.event("error_callback",OH_AVScreenCapture_SetErrorCallback(capture,onError,&state));
    state.event("data_callback",OH_AVScreenCapture_SetDataCallback(capture,onData,&state));
    const auto init=OH_AVScreenCapture_Init(capture,config); state.event("init",init);
    int started=-1;
    if(init==0) {
        started=OH_AVScreenCapture_StartScreenCapture(capture); state.event("start",started);
        if(started==0) {
            const int64_t deadline=ticks(CLOCK_BOOTTIME)+duration*second;
            while(ticks(CLOCK_BOOTTIME)<deadline && access((state.directory+"/stop").c_str(),F_OK)!=0) usleep(100000);
            stopped.store(true);
            state.event("stop",OH_AVScreenCapture_StopScreenCapture(capture));
        }
    }
    state.event("release",OH_AVScreenCapture_Release(capture));
    state.event("frames",static_cast<int>(state.frames));
    state.event("errors",static_cast<int>(state.errors));
    deflateEnd(&state.deflater); std::fclose(state.log);
    return init==0 && started==0 && state.frames>0 && state.errors==0?0:6;
}
